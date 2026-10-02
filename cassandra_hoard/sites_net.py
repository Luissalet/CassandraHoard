"""The network side of the public-site checks: HTTP, TLS, DNS and RDAP.

Every function takes plain arguments and returns a plain dict, never raises
and is injectable into :class:`cassandra_hoard.sites.SiteWatcher`, so the
tests replace each one with a fake.

Politeness: one GET of the home page per check (at most 512 KB of body is
read, redirects are only followed when a keyword has to be found), a clear
User-Agent, one TLS handshake and one DNS lookup. Domain registration data
comes from RDAP, at most once a day per registrable domain.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import ssl
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from . import __version__
from .hoard_link.web import fetch, urls

USER_AGENT = f"Cassandra's Hoard site check/{__version__} (monitor run by the site owner)"
HTTP_TIMEOUT_S = 15.0
TLS_TIMEOUT_S = 10.0
DNS_TIMEOUT_S = 5.0
MAX_BODY = 512 * 1024
MAX_HOPS = 5

RDAP_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
RDAP_FALLBACK_URL = "https://rdap.org/domain/"
RDAP_EVERY_S = 86400.0  # one lookup per registrable domain per day
RDAP_RETRY_S = 6 * 3600.0  # after a transport failure
RDAP_BOOTSTRAP_TTL_S = 7 * 86400.0
RDAP_TIMEOUT_S = 10.0

# ---------------------------------------------------------------------------
# domains
# ---------------------------------------------------------------------------

def registrable_domain(host: str, override: str = "") -> Optional[str]:
    """``www.shop.example.co.uk`` -> ``example.co.uk`` (the shared public-suffix table); None for an IP address or a
    single label (nothing to look up). A configured ``override`` wins."""
    override = (override or "").strip().lower().strip(".")
    if override:
        return override
    host = (host or "").strip().lower().strip(".").strip("[]")
    if not host or "." not in host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    return urls.registrable_domain(host)


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

def dns_check(host: str, port: int = 443, *, timeout: float = DNS_TIMEOUT_S) -> dict[str, Any]:
    """``{ok, addresses (sorted A/AAAA answers), error}``. The lookup runs in a thread so it cannot hang the watcher."""
    host = host.strip("[]")
    try:
        ipaddress.ip_address(host)
        return {"ok": True, "addresses": [host], "error": None}
    except ValueError:
        pass
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            box["addresses"] = sorted({str(i[4][0]) for i in infos})
        except Exception as error:  # noqa: BLE001
            box["error"] = error

    thread = threading.Thread(target=work, name="cassandra-dns", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return {"ok": False, "addresses": [], "error": f"DNS failure: no answer for {host} within {timeout:.0f} s"}
    if "error" in box:
        error = box["error"]
        reason = getattr(error, "strerror", None) or str(error)
        return {"ok": False, "addresses": [], "error": f"DNS failure: {host} does not resolve ({reason})"}
    return {"ok": bool(box.get("addresses")), "addresses": box.get("addresses", []),
            "error": None if box.get("addresses") else f"DNS failure: {host} has no A/AAAA record"}

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def classify_error(error: BaseException, host: str, timeout: float) -> dict[str, str]:
    """An exception from httpx or the ssl module -> ``{kind, detail}`` with kind dns|tls|timeout|refused|reset|other.

    The classification is the shared fetcher's (it also reads a dropped HTTP connection as ``reset``); this keeps
    the monitor's dict shape and its ``other`` kind (the shared ``network``)."""
    kind, detail = fetch.classify_error(error, host, timeout)
    return {"kind": "other" if kind == "network" else kind, "detail": detail}


def http_check(url: str, *, timeout: float = HTTP_TIMEOUT_S, follow: bool = False,
               transport: Optional[httpx.BaseTransport] = None) -> dict[str, Any]:
    """GET the home page once. With ``follow`` (a keyword must be found) up to five redirects are followed.

    Returns ``{status, latency_ms, redirect, final_url, body, error}``: ``status`` is the last response's, ``redirect``
    the first ``Location`` seen, ``body`` the text of the last response (None for a redirect that was not followed).
    """
    host = urlsplit(url).hostname or url
    started = time.perf_counter()
    out: dict[str, Any] = {"status": None, "latency_ms": None, "redirect": None, "final_url": url, "body": None, "error": None}
    current = url
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, transport=transport,
                          headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}) as client:
            for hop in range(MAX_HOPS + 1):
                with client.stream("GET", current) as response:
                    out["status"] = response.status_code
                    out["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
                    out["final_url"] = current
                    location = response.headers.get("location")
                    if 300 <= response.status_code < 400 and location:
                        target = urljoin(current, location)
                        if out["redirect"] is None:
                            out["redirect"] = target
                        if follow and hop < MAX_HOPS:
                            current = target
                            continue
                        break
                    received = bytearray()
                    for chunk in response.iter_bytes():
                        received.extend(chunk)
                        if len(received) >= MAX_BODY:
                            break
                    out["body"] = bytes(received[:MAX_BODY]).decode(response.encoding or "utf-8", errors="replace")
                    break
    except Exception as error:  # noqa: BLE001 - classified, never raised
        out["status"] = None
        out["latency_ms"] = None
        out["body"] = None
        out["error"] = classify_error(error, host, timeout)
    return out


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

def _name(parts: Any) -> str:
    """The commonName (or organizationName) out of an ssl-module name tuple."""
    flat: dict[str, str] = {}
    for rdn in parts or ():
        for key, value in rdn:
            flat.setdefault(key, value)
    org, cn = flat.get("organizationName"), flat.get("commonName")
    if org and cn and org != cn:
        return f"{org} ({cn})"
    return org or cn or ""


def _decode_der(der: bytes) -> dict[str, Any]:
    """Decode a DER certificate that failed verification (the stdlib only decodes verified ones)."""
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
        handle, path = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(handle, "w", encoding="ascii") as fh:
                fh.write(pem)
            return dict(ssl._ssl._test_decode_cert(path))  # type: ignore[attr-defined]  # noqa: SLF001
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        return {}


def _fill_cert(out: dict[str, Any], cert: dict[str, Any], der: Optional[bytes], now: float) -> None:
    import hashlib
    try:
        not_after = ssl.cert_time_to_seconds(cert["notAfter"])
        out["not_after"] = not_after
        out["days_left"] = int((not_after - now) // 86400)
    except (KeyError, ValueError):
        pass
    try:
        out["not_before"] = ssl.cert_time_to_seconds(cert["notBefore"])
    except (KeyError, ValueError):
        pass
    out["issuer"] = _name(cert.get("issuer"))
    out["subject"] = _name(cert.get("subject"))
    out["san"] = [value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"][:20]
    if der:
        out["fingerprint"] = hashlib.sha256(der).hexdigest()[:32]


def tls_check(host: str, port: int = 443, *, timeout: float = TLS_TIMEOUT_S, now: Optional[float] = None) -> dict[str, Any]:
    """One TLS handshake: certificate end date, days left, issuer, hostname match and whether the chain is valid.

    ``{ok, chain_valid, hostname_match, not_after, days_left, issuer, subject, san, fingerprint, error}``; ``ok`` means the
    handshake verified (valid chain and matching host name). A certificate that fails verification is still read, so the
    expiry date is known even when the chain is broken.
    """
    now = time.time() if now is None else now
    host = host.strip("[]")
    out: dict[str, Any] = {"ok": False, "chain_valid": None, "hostname_match": None, "not_after": None, "not_before": None,
                           "days_left": None, "issuer": "", "subject": "", "san": [], "fingerprint": None, "error": None}
    try:
        with socket.create_connection((host, port), timeout) as sock:
            context = ssl.create_default_context()
            with context.wrap_socket(sock, server_hostname=host) as tls:
                _fill_cert(out, tls.getpeercert() or {}, tls.getpeercert(binary_form=True), now)
        out.update(ok=True, chain_valid=True, hostname_match=True)
        return out
    except ssl.SSLCertVerificationError as error:
        mismatch = error.verify_code in (62, 64)
        out["chain_valid"] = None if mismatch else False
        out["hostname_match"] = False if mismatch else None
        out["error"] = f"TLS error: {error.verify_message or error.reason or error}"
    except ssl.SSLError as error:
        out["error"] = f"TLS error: {error.reason or error}"
        return out
    except (OSError, socket.timeout) as error:
        out["error"] = classify_error(error, host, timeout)["detail"]
        return out
    # The certificate did not verify: read it anyway (no verification) for its dates and issuer.
    try:
        unverified = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout) as sock:
            with unverified.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        if der:
            _fill_cert(out, _decode_der(der), der, now)
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------
# RDAP (domain registration expiry)
# ---------------------------------------------------------------------------

def _parse_date(value: Any) -> Optional[float]:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _registrar(data: dict[str, Any]) -> str:
    for entity in data.get("entities") or []:
        if not isinstance(entity, dict) or "registrar" not in [str(r).lower() for r in entity.get("roles") or []]:
            continue
        vcard = entity.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) > 1:
            for item in vcard[1]:
                if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
                    return str(item[3])[:120]
        for ident in entity.get("publicIds") or []:
            if isinstance(ident, dict) and ident.get("identifier"):
                return str(ident["identifier"])[:120]
    return ""


class RdapChecker:
    """Registration expiry of a domain, cached in ``rdap_cache``.

    The IANA bootstrap file (cached a week in ``settings``) names the TLD's RDAP server; ``rdap.org`` is the fallback.
    A lookup happens at most once a day per domain (six hours after a transport failure). Absence of RDAP, or of an
    expiration date in the answer, is an answer: status ``unknown`` with the reason.
    """

    def __init__(self, db, *, client: Optional[httpx.Client] = None, clock_fn: Callable[[], float] = time.time):
        self.db = db
        self._client = client
        self.clock = clock_fn
        self._lock = threading.Lock()

    # ----- cache
    def cached(self, domain: str) -> Optional[dict[str, Any]]:
        row = self.db.one("SELECT * FROM rdap_cache WHERE domain = ?", (domain,))
        if row is None:
            return None
        out = dict(row)
        try:
            out["warned"] = json.loads(out.get("warned") or "{}")
        except ValueError:
            out["warned"] = {}
        return out

    def _store(self, domain: str, now: float, status: str, expires_at: Optional[float], registrar: str, detail: str, source: str) -> dict[str, Any]:
        previous = self.cached(domain)
        warned = json.dumps((previous or {}).get("warned") or {})
        self.db.execute(
            "INSERT INTO rdap_cache(domain, checked_at, status, expires_at, registrar, detail, source, warned) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET checked_at = excluded.checked_at, status = excluded.status, expires_at = excluded.expires_at, "
            "registrar = excluded.registrar, detail = excluded.detail, source = excluded.source",
            (domain, now, status, expires_at, registrar, detail[:300], source[:300], warned),
        )
        return self.cached(domain) or {}

    def mark_warned(self, domain: str, key: str, thresholds: list[int]) -> None:
        row = self.cached(domain)
        if row is None:
            return
        warned = row["warned"]
        warned[key] = sorted(set(warned.get(key, [])) | set(thresholds))
        for old in list(warned)[:-4]:  # keep the latest few expiry dates only
            warned.pop(old, None)
        self.db.execute("UPDATE rdap_cache SET warned = ? WHERE domain = ?", (json.dumps(warned), domain))

    # ----- lookup
    def due(self, row: Optional[dict[str, Any]], now: float) -> bool:
        if row is None:
            return True
        limit = RDAP_RETRY_S if row["status"] == "error" else RDAP_EVERY_S
        return now - float(row["checked_at"]) >= limit

    def get(self, domain: str, now: Optional[float] = None) -> dict[str, Any]:
        """The cached answer, refreshed first when it is due. Never raises."""
        now = self.clock() if now is None else now
        with self._lock:
            row = self.cached(domain)
            if not self.due(row, now):
                return row or {}
            try:
                return self.lookup(domain, now)
            except Exception as error:  # noqa: BLE001
                return self._store(domain, now, "error", None, "", f"RDAP lookup failed: {type(error).__name__}", "")

    def _http(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.Client(timeout=RDAP_TIMEOUT_S, follow_redirects=True, trust_env=False,
                            headers={"Accept": "application/rdap+json, application/json", "User-Agent": USER_AGENT}), True

    def _bases(self, client: httpx.Client, tld: str, now: float) -> list[str]:
        raw = self.db.get_setting("rdap_bootstrap")  # a dict (new rows) or JSON text (rows written before the shared database helper)
        boot: Optional[dict[str, Any]] = raw if isinstance(raw, dict) else None
        if boot is None and isinstance(raw, str) and raw:
            try:
                boot = json.loads(raw)
            except ValueError:
                boot = None
        if not isinstance(boot, dict):
            boot = None
        if boot is None or now - float(boot.get("fetched_at", 0)) >= RDAP_BOOTSTRAP_TTL_S:
            try:
                response = client.get(RDAP_BOOTSTRAP_URL)
                if response.status_code == 200:
                    boot = {"fetched_at": now, "services": response.json().get("services", [])}
                    self.db.set_setting("rdap_bootstrap", boot)
            except (httpx.HTTPError, ValueError):
                pass  # keep a stale copy when there is one
        bases: list[str] = []
        for entry in (boot or {}).get("services", []):
            try:
                tlds, urls = entry[0], entry[1]
            except (IndexError, TypeError):
                continue
            if tld in [str(t).lower() for t in tlds]:
                bases = sorted((str(u) for u in urls), key=lambda u: not u.startswith("https://"))
                break
        return bases

    def lookup(self, domain: str, now: float) -> dict[str, Any]:
        client, owned = self._http()
        try:
            tld = domain.rsplit(".", 1)[-1].lower()
            bases = self._bases(client, tld, now)
            urls = [base.rstrip("/") + "/domain/" + domain for base in bases] + [RDAP_FALLBACK_URL + domain]
            last_error = "no RDAP server answered"
            for url in urls:
                try:
                    response = client.get(url)
                except httpx.HTTPError as error:
                    last_error = f"RDAP request failed ({type(error).__name__})"
                    continue
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        last_error = "RDAP answer is not JSON"
                        continue
                    return self._parse(domain, data, url, now)
                if response.status_code == 404:
                    detail = "the registry's RDAP server has no record of this domain" if bases else f"no RDAP service known for .{tld}"
                    return self._store(domain, now, "unknown", None, "", detail, url)
                last_error = f"RDAP HTTP {response.status_code}"
            return self._store(domain, now, "error", None, "", last_error, "")
        finally:
            if owned:
                client.close()

    def _parse(self, domain: str, data: Any, url: str, now: float) -> dict[str, Any]:
        if not isinstance(data, dict):
            return self._store(domain, now, "unknown", None, "", "RDAP answer has an unexpected shape", url)
        expires = None
        for event in data.get("events") or []:
            if isinstance(event, dict) and str(event.get("eventAction", "")).lower() == "expiration":
                expires = _parse_date(event.get("eventDate"))
                if expires:
                    break
        if expires is None:
            return self._store(domain, now, "unknown", None, _registrar(data), "the RDAP answer has no expiration date", url)
        return self._store(domain, now, "ok", expires, _registrar(data), "", url)
