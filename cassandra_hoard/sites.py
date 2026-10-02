"""Public sites ("Webs publicas"): the websites other people use, watched from outside.

The list lives in ``data/sites.json`` (never in code): id, url, expected
status, optional keyword, check interval. Each check is one DNS lookup, one
GET of the home page and, at most hourly, one TLS handshake; the registration
expiry of the registrable domain comes from RDAP at most once a day (cached).

A site is **down** after ``DOWN_AFTER`` consecutive failed checks (one failure
only schedules a confirming check a minute later) and **up** again on the first
success. Those changes use the same stores as the services: ``samples`` and
``events`` (service ``site:<id>``), and ``incidents`` with kind ``site`` and a
plain-words cause (DNS failure, TLS error, timeout, HTTP 5xx, keyword missing).
Certificate warnings (21, 7 and 1 days left) and domain warnings (30, 7 and 1)
are announced once per certificate / per expiry date.

Bus events: ``cassandra.site.down``, ``.up``, ``.cert_expiring``, ``.domain_expiring``.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from . import sites_net, views
from .hoard_link import atomic
from .times import clock, duration, iso

SITE_PREFIX = "site:"
DEFAULT_INTERVAL_MIN = 5.0
MIN_INTERVAL_MIN = 1.0
MAX_INTERVAL_MIN = 1440.0
DEFAULT_EXPECT = ("2xx", "3xx")
DOWN_AFTER = 2
CONFIRM_RETRY_S = 60.0
MANUAL_MIN_GAP_S = 60.0
TLS_EVERY_S = 3600.0
CERT_WARN_DAYS = (21, 7, 1)
DOMAIN_WARN_DAYS = (30, 7, 1)
TICK_S = 15.0

EV_DOWN = "cassandra.site.down"
EV_UP = "cassandra.site.up"
EV_CERT = "cassandra.site.cert_expiring"
EV_DOMAIN = "cassandra.site.domain_expiring"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def normalize_url(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("A site needs a url, for example https://example.com/.")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The url must be http:// or https:// with a host name.")
    if parts.username or parts.password:
        raise ValueError("Credentials inside the url are not allowed.")
    try:
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError as error:
        raise ValueError("The url has an invalid port.") from error
    return parts._replace(netloc=parts.netloc.lower(), path=parts.path or "/", fragment="").geturl()


def normalize_expect(value: Any) -> list[str]:
    """``None`` -> the default; ``["2xx", "301", "200-299"]`` or ``"2xx,3xx"`` -> a clean list."""
    if value in (None, "", []):
        return list(DEFAULT_EXPECT)
    if isinstance(value, str):
        items = [p for p in re.split(r"[,\s]+", value) if p]
    elif isinstance(value, (list, tuple)):
        items = [str(p).strip() for p in value if str(p).strip()]
    else:
        raise ValueError('expect_status must be a list such as ["2xx", "3xx"] or ["200", "301-308"].')
    out: list[str] = []
    for item in items:
        low = item.lower()
        span = re.fullmatch(r"([1-5]\d\d)-([1-5]\d\d)", low)
        if re.fullmatch(r"[1-5]xx", low) or re.fullmatch(r"[1-5]\d\d", low):
            out.append(low)
        elif span and int(span.group(1)) <= int(span.group(2)):
            out.append(low)
        else:
            raise ValueError(f"'{item}' is not a status pattern (use 2xx, 301 or 200-299).")
    return out or list(DEFAULT_EXPECT)


def status_ok(code: Optional[int], expect: list[str]) -> bool:
    if code is None:
        return False
    for item in expect:
        if item.endswith("xx") and code // 100 == int(item[0]):
            return True
        if "-" in item:
            low, high = item.split("-")
            if int(low) <= code <= int(high):
                return True
        elif item.isdigit() and code == int(item):
            return True
    return False


def slug_for(url: str) -> str:
    host = (urlsplit(url).hostname or "site").lower()
    if host.startswith("www."):
        host = host[4:]
    return re.sub(r"[^a-z0-9._-]+", "-", host).strip("-._")[:64] or "site"


def validate_site(raw: dict[str, Any], existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Merge ``raw`` over ``existing`` and return the clean entry; ValueError says what is wrong."""
    base = dict(existing or {})
    for key, value in raw.items():
        if value is not None:
            base[key] = value
    url = normalize_url(base.get("url"))
    site_id = str(base.get("id") or slug_for(url)).strip().lower()
    if not ID_RE.match(site_id):
        raise ValueError("The id may only use lowercase letters, digits, '.', '_' and '-' (up to 64).")
    try:
        interval = float(base.get("interval_min", DEFAULT_INTERVAL_MIN))
    except (TypeError, ValueError) as error:
        raise ValueError("interval_min must be a number of minutes.") from error
    if not MIN_INTERVAL_MIN <= interval <= MAX_INTERVAL_MIN:
        raise ValueError(f"interval_min must be between {MIN_INTERVAL_MIN:g} and {MAX_INTERVAL_MIN:g} minutes.")
    keyword = str(base.get("keyword") or "").strip()
    if len(keyword) > 200:
        raise ValueError("The keyword is limited to 200 characters.")
    host = urlsplit(url).hostname or ""
    return {
        "id": site_id,
        "name": str(base.get("name") or "").strip()[:120] or host,
        "url": url,
        "expect_status": normalize_expect(base.get("expect_status")),
        "keyword": keyword,
        "interval_min": interval,
        "enabled": bool(base.get("enabled", True)),
        "domain": str(base.get("domain") or "").strip().lower().strip(".")[:253],
    }


# ---------------------------------------------------------------------------
# the list (data/sites.json)
# ---------------------------------------------------------------------------

class SiteStore:
    """``{"sites": [...]}`` in a JSON file; edits made by hand are picked up on the next read."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._sites: list[dict[str, Any]] = []
        self._stamp: Optional[tuple[int, int]] = None
        self.load_error: Optional[str] = None
        self._refresh(force=True)

    def _fingerprint(self) -> Optional[tuple[int, int]]:
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def _refresh(self, force: bool = False) -> None:
        stamp = self._fingerprint()
        if not force and stamp == self._stamp:
            return
        self._stamp = stamp
        if stamp is None:
            self._sites, self.load_error = [], None
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            self.load_error = f"sites.json cannot be read: {error}"  # keep the last good list
            return
        entries = raw.get("sites") if isinstance(raw, dict) else raw
        clean: list[dict[str, Any]] = []
        problems: list[str] = []
        seen: set[str] = set()
        for entry in entries if isinstance(entries, list) else []:
            try:
                site = validate_site(entry if isinstance(entry, dict) else {})
            except ValueError as error:
                problems.append(f"{(entry or {}).get('id', '?') if isinstance(entry, dict) else '?'}: {error}")
                continue
            if site["id"] in seen:
                problems.append(f"{site['id']}: duplicated id")
                continue
            seen.add(site["id"])
            clean.append(site)
        self._sites = clean
        self.load_error = "; ".join(problems) or None

    def _write(self) -> None:
        atomic.write_json_atomic(self.path, {"sites": self._sites})
        self._stamp = self._fingerprint()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            return [dict(s) for s in self._sites]

    def find(self, text: str) -> Optional[dict[str, Any]]:
        """By id, ``site:<id>``, name, host or url (case-insensitive)."""
        wanted = (text or "").strip().lower()
        if wanted.startswith(SITE_PREFIX):
            wanted = wanted[len(SITE_PREFIX):]
        for site in self.list():
            host = (urlsplit(site["url"]).hostname or "").lower()
            if wanted in (site["id"], site["name"].lower(), host, site["url"].lower(), site["url"].lower().rstrip("/")):
                return site
        return None

    def upsert(self, raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Add or edit; returns ``(site, created)``. An id (or name, or host) that exists is edited."""
        with self._lock:
            self._refresh()
            probe = raw.get("id") or raw.get("url") or ""
            existing = self.find(str(probe)) if probe else None
            if existing is not None:
                patch = {k: v for k, v in raw.items() if k != "id"}
                site = validate_site(patch, existing)
                self._sites = [site if s["id"] == existing["id"] else s for s in self._sites]
                created = False
            else:
                site = validate_site(raw)
                if any(s["id"] == site["id"] for s in self._sites):
                    raise ValueError(f"The id '{site['id']}' is already used by another site.")
                self._sites.append(site)
                created = True
            self._write()
            return dict(site), created

    def remove(self, text: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._refresh()
            site = self.find(text)
            if site is None:
                return None
            self._sites = [s for s in self._sites if s["id"] != site["id"]]
            self._write()
            return site


# ---------------------------------------------------------------------------
# the watcher
# ---------------------------------------------------------------------------

def _blank_state() -> dict[str, Any]:
    return {
        "state": "unknown", "since": None, "fail_streak": 0, "status": None, "latency_ms": None, "redirect": None,
        "keyword_ok": None, "cause": None, "cause_code": None, "last_check": None, "next_due": 0.0, "checks": 0,
        "tls": None, "tls_checked_at": None, "dns": {"addresses": [], "changed_at": None, "changes": 0, "error": None},
        "cert_warned": {}, "cert_key": None, "last_change": None, "incident_id": None,
    }


def evaluate(site: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str, str]:
    """One HTTP result against the site's expectations -> ``(ok, cause_code, cause_text)``."""
    error = result.get("error")
    if error:
        return False, error["kind"], error["detail"]
    status = result.get("status")
    if not status_ok(status, site["expect_status"]):
        expected = ", ".join(site["expect_status"])
        if status is not None and status >= 500:
            return False, "http_5xx", f"HTTP {status} (server error); expected {expected}"
        return False, "http_status", f"HTTP {status} (unexpected status); expected {expected}"
    keyword = site.get("keyword")
    if keyword:
        body = result.get("body")
        if body is None or keyword.lower() not in body.lower():
            return False, "keyword", f"The keyword '{keyword}' is missing from the home page (HTTP {status})"
    return True, "ok", ""


class SiteWatcher:
    """Runs the checks (a thread, every ``tick_s`` looking for due sites) and keeps each site's state."""

    def __init__(self, db, store: SiteStore, *, incidents, emit: Callable[[str, dict[str, Any]], None],
                 clock_fn: Callable[[], float] = time.time, tick_s: float = TICK_S, enabled: bool = True,
                 http_fn: Optional[Callable[..., dict[str, Any]]] = None, tls_fn: Optional[Callable[..., dict[str, Any]]] = None,
                 dns_fn: Optional[Callable[..., dict[str, Any]]] = None, rdap: Optional[Any] = None, rdap_client: Optional[Any] = None,
                 down_after: int = DOWN_AFTER, confirm_retry_s: float = CONFIRM_RETRY_S, manual_gap_s: float = MANUAL_MIN_GAP_S):
        self.db = db
        self.store = store
        self.incidents = incidents
        self.emit = emit
        self.clock = clock_fn
        self.tick_s = max(1.0, float(tick_s))
        self.enabled = enabled
        self.http_fn = http_fn or sites_net.http_check
        self.tls_fn = tls_fn or sites_net.tls_check
        self.dns_fn = dns_fn or sites_net.dns_check
        self.rdap = rdap if rdap is not None else sites_net.RdapChecker(db, client=rdap_client, clock_fn=clock_fn)
        self.down_after = max(1, int(down_after))
        self.confirm_retry_s = float(confirm_retry_s)
        self.manual_gap_s = float(manual_gap_s)
        self.states: dict[str, dict[str, Any]] = {}
        self.last_tick: Optional[float] = None
        self.last_error: Optional[str] = None
        self.sent = 0
        self._check_lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load_states()

    # ----- persistence
    def _load_states(self) -> None:
        for row in self.db.query("SELECT site, data FROM site_state"):
            try:
                loaded = json.loads(row["data"])
            except ValueError:
                continue
            state = _blank_state()
            state.update(loaded)
            self.states[row["site"]] = state

    def state_of(self, site_id: str) -> dict[str, Any]:
        return self.states.get(site_id) or _blank_state()

    def _save(self, site_id: str, state: dict[str, Any]) -> None:
        self.states[site_id] = state
        self.db.execute(
            "INSERT INTO site_state(site, data, updated_at) VALUES (?, ?, ?) ON CONFLICT(site) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (site_id, json.dumps(state, ensure_ascii=False), self.clock()),
        )

    def name_of(self, service_id: str) -> str:
        site = self.store.find(service_id)
        return site["name"] if site else service_id[len(SITE_PREFIX):] if service_id.startswith(SITE_PREFIX) else service_id

    # ----- thread
    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cassandra-sites", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as error:  # noqa: BLE001 - one bad tick never stops the watch
                self.last_error = f"{type(error).__name__}: {error}"
            self._wake.wait(self.tick_s)
            self._wake.clear()

    def tick(self) -> list[str]:
        """Check every enabled site whose time has come; returns the ids checked."""
        done: list[str] = []
        for site in self.store.list():
            if not site["enabled"]:
                continue
            if self.clock() < self.state_of(site["id"])["next_due"]:
                continue
            try:
                self.check(site)
            except Exception as error:  # noqa: BLE001
                self.last_error = f"{site['id']}: {type(error).__name__}: {error}"
            done.append(site["id"])
        self.last_tick = self.clock()
        return done

    def check_now(self, ident: Optional[str] = None) -> list[dict[str, Any]]:
        """Manual check (the Panel button): sites checked less than ``manual_gap_s`` ago are not asked again."""
        sites = [self.store.find(ident)] if ident else self.store.list()
        out = []
        for site in sites:
            if site is None:
                raise LookupError(f"Unknown site '{ident}'.")
            if not site["enabled"]:
                continue
            last = self.state_of(site["id"])["last_check"]
            if last is not None and self.clock() - last < self.manual_gap_s:
                out.append({"id": site["id"], "checked": False, "reason": f"checked {duration(self.clock() - last)} ago (not more than once a minute)"})
                continue
            self.check(site)
            out.append({"id": site["id"], "checked": True})
        return out

    def forget(self, site: dict[str, Any]) -> None:
        """A site was removed from the list: close its open incident; its history stays."""
        service = SITE_PREFIX + site["id"]
        self.incidents.close(service, self.clock(), "removed")
        self.states.pop(site["id"], None)
        self.db.execute("DELETE FROM site_state WHERE site = ?", (site["id"],))

    # ----- the check
    def _call(self, fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 - a broken probe is a failed check, never a crash
            return {"ok": False, "error": {"kind": "other", "detail": f"{type(error).__name__}: {error}"}}

    def _event(self, service: str, ts: float, kind: str, from_state: Optional[str], to_state: Optional[str], detail: str) -> None:
        self.db.execute("INSERT INTO events(service, ts, kind, from_state, to_state, detail) VALUES (?, ?, ?, ?, ?, ?)",
                        (service, ts, kind, from_state, to_state, detail[:500]))

    def _announce(self, type_: str, data: dict[str, Any]) -> None:
        try:
            self.emit(type_, data)
            self.sent += 1
        except Exception:  # noqa: BLE001 - a broken transport never stops the watch
            pass

    def check(self, site: dict[str, Any]) -> dict[str, Any]:
        """One full check of ``site`` (DNS, HTTP, TLS when due, domain when due); updates and stores its state."""
        with self._check_lock:
            now = self.clock()
            sid = site["id"]
            service = SITE_PREFIX + sid
            st = copy.deepcopy(self.state_of(sid))
            parts = urlsplit(site["url"])
            host = parts.hostname or ""
            https = parts.scheme == "https"
            port = parts.port or (443 if https else 80)

            dns = self._call(self.dns_fn, host, port)
            if dns.get("ok"):
                result = self._call(self.http_fn, site["url"], follow=bool(site.get("keyword")))
            else:
                result = {"status": None, "latency_ms": None, "redirect": None, "body": None,
                          "error": {"kind": "dns", "detail": dns.get("error") or f"DNS failure: {host} does not resolve"}}
            ok, code, cause = evaluate(site, result)

            tls = st["tls"]
            tls_error = bool((result.get("error") or {}).get("kind") == "tls")
            if https and dns.get("ok") and (tls is None or tls_error or not tls.get("ok") or now - (st["tls_checked_at"] or 0) >= TLS_EVERY_S):
                tls = self._call(self.tls_fn, host, port)
                st["tls"], st["tls_checked_at"] = tls, now
            elif not https:
                st["tls"] = tls = None

            # DNS answers: record the set, flag a change.
            if dns.get("ok"):
                addresses = list(dns.get("addresses") or [])
                previous = st["dns"].get("addresses") or []
                if previous and addresses != previous:
                    self._event(service, now, "dns", None, None, f"A/AAAA answers changed: {', '.join(previous)} → {', '.join(addresses)}")
                    st["dns"]["changed_at"] = now
                    st["dns"]["changes"] = int(st["dns"].get("changes") or 0) + 1
                st["dns"]["addresses"], st["dns"]["error"] = addresses, None
            else:
                st["dns"]["error"] = dns.get("error")

            # The state machine: down after `down_after` failures in a row, up on the first success.
            prev = st["state"]
            if ok:
                st["fail_streak"], new = 0, "up"
            else:
                st["fail_streak"] = int(st["fail_streak"]) + 1
                new = "down" if st["fail_streak"] >= self.down_after else prev
            st.update(status=result.get("status"), latency_ms=result.get("latency_ms"), redirect=result.get("redirect"),
                      keyword_ok=(None if not site.get("keyword") or result.get("error") or result.get("status") is None
                                  else code != "keyword"),
                      cause=None if ok else cause, cause_code=None if ok else code, last_check=now, checks=int(st["checks"]) + 1)
            st["state"] = new
            if new in ("up", "down"):
                pending = not ok and new != "down"
                detail = ("unconfirmed failure: " + cause) if pending else (cause if not ok else "")
                self.db.execute(
                    "INSERT INTO samples(service, ts, state, latency_ms, detail, pid, pid_started, cmd_hash) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)",
                    (service, now, new, result.get("latency_ms") if ok else None, detail[:500]))
            if new != prev:
                self._transition(site, st, prev, new, now, ok, cause, code, result)
            elif new == "down" and not ok and code != st.get("down_code"):
                if st.get("incident_id"):
                    self.incidents.add_action(st["incident_id"], {"ts": now, "kind": "cause_changed", "detail": cause})
            if new == "down":
                st["down_code"] = code

            self._cert_alerts(site, st, now)
            self._domain_alerts(site, host, now)

            interval = float(site["interval_min"]) * 60
            confirming = not ok and st["fail_streak"] < self.down_after
            st["next_due"] = now + (min(interval, self.confirm_retry_s) if confirming else interval)
            self._save(sid, st)
            return st

    def _transition(self, site: dict[str, Any], st: dict[str, Any], prev: str, new: str, now: float, ok: bool,
                    cause: str, code: str, result: dict[str, Any]) -> None:
        service = SITE_PREFIX + site["id"]
        base = {"site": site["id"], "name": site["name"], "url": site["url"]}
        if prev == "unknown" and new == "up":
            st["since"] = now  # first look: nothing changed, nothing to announce
            return
        detail = cause if new == "down" else f"answers again (HTTP {result.get('status')}, {result.get('latency_ms')} ms)"
        self._event(service, now, "state", prev, new, detail)
        if new == "down":
            existing = self.incidents.open_for(service)
            if existing is not None:
                incident_id = existing["id"]
            else:
                incident_id = self.incidents.open_site(
                    service, now, from_state=prev, detail=cause, cause_code=code, cause_text=cause,
                    site={"url": site["url"], "status": result.get("status"), "keyword": site.get("keyword") or None,
                          "dns": st["dns"].get("addresses"), "streak": st["fail_streak"]})
            st["incident_id"] = incident_id
            st["last_change"] = {"at": now, "from": prev, "to": new, "cause": cause}
            self._announce(EV_DOWN, {**base, "cause": cause, "cause_code": code, "status": result.get("status"), "incident_id": incident_id})
        else:
            incident_id = st.get("incident_id")
            down_since = st.get("since") if prev == "down" else None
            self.incidents.close(service, now, "up", incident_id=incident_id) if incident_id else self.incidents.close(service, now, "up")
            st["last_change"] = {"at": now, "from": prev, "to": new, "cause": None}
            st["incident_id"] = None
            st.pop("down_code", None)
            self._announce(EV_UP, {**base, "status": result.get("status"), "latency_ms": result.get("latency_ms"),
                                   "downtime_s": round(now - down_since, 1) if down_since else None, "incident_id": incident_id})
        st["since"] = now

    # ----- warnings (once per certificate / expiry date)
    def _cert_alerts(self, site: dict[str, Any], st: dict[str, Any], now: float) -> None:
        tls = st.get("tls")
        if not tls or tls.get("days_left") is None or tls.get("not_after") is None:
            return
        service = SITE_PREFIX + site["id"]
        key = str(tls.get("fingerprint") or int(tls["not_after"]))
        previous_key = st.get("cert_key")
        if previous_key and previous_key != key:
            self._event(service, now, "cert", None, None,
                        f"certificate changed: now valid until {iso(tls['not_after'])} (issuer {tls.get('issuer') or '?'})")
        st["cert_key"] = key
        days = int(tls["days_left"])
        crossed = [t for t in CERT_WARN_DAYS if days <= t]
        warned = st["cert_warned"]
        if crossed and not set(crossed) <= set(warned.get(key, [])):
            threshold = min(crossed)
            warned[key] = sorted(set(warned.get(key, [])) | set(crossed))
            for old in list(warned)[:-4]:
                warned.pop(old, None)
            text = (f"certificate expires in {days} day(s) (on {iso(tls['not_after'])}, warning at {threshold})" if days >= 0
                    else f"certificate expired {-days} day(s) ago (on {iso(tls['not_after'])})")
            self._event(service, now, "cert", None, None, text)
            self._announce(EV_CERT, {"site": site["id"], "name": site["name"], "url": site["url"], "days_left": days, "threshold": threshold,
                                     "not_after": iso(tls["not_after"]), "issuer": tls.get("issuer") or ""})

    def _domain_alerts(self, site: dict[str, Any], host: str, now: float) -> None:
        domain = sites_net.registrable_domain(host, site.get("domain", ""))
        if not domain:
            return
        row = self.rdap.get(domain, now)
        if not row or row.get("status") != "ok" or row.get("expires_at") is None:
            return
        days = int((float(row["expires_at"]) - now) // 86400)
        crossed = [t for t in DOMAIN_WARN_DAYS if days <= t]
        key = str(int(float(row["expires_at"]) // 86400))
        warned = (row.get("warned") or {}).get(key, [])
        if not crossed or set(crossed) <= set(warned):
            return
        threshold = min(crossed)
        self.rdap.mark_warned(domain, key, crossed)
        sharing = [s["id"] for s in self.store.list() if sites_net.registrable_domain(urlsplit(s["url"]).hostname or "", s.get("domain", "")) == domain]
        text = (f"domain {domain} expires in {days} day(s) (on {iso(float(row['expires_at']))}, warning at {threshold})" if days >= 0
                else f"domain {domain} expired {-days} day(s) ago")
        for sid in sharing or [site["id"]]:
            self._event(SITE_PREFIX + sid, now, "domain", None, None, text)
        self._announce(EV_DOMAIN, {"domain": domain, "sites": sharing or [site["id"]], "days_left": days, "threshold": threshold,
                                   "expires": iso(float(row["expires_at"])), "registrar": row.get("registrar") or ""})

    # ----- views
    def _tls_view(self, st: dict[str, Any]) -> Optional[dict[str, Any]]:
        tls = st.get("tls")
        if not tls:
            return None
        return {"ok": tls.get("ok"), "days_left": tls.get("days_left"), "not_after": iso(tls["not_after"]) if tls.get("not_after") else None,
                "issuer": tls.get("issuer") or None, "hostname_match": tls.get("hostname_match"), "chain_valid": tls.get("chain_valid"),
                "error": tls.get("error"), "checked": iso(st["tls_checked_at"]) if st.get("tls_checked_at") else None}

    def _domain_view(self, site: dict[str, Any], now: float) -> Optional[dict[str, Any]]:
        domain = sites_net.registrable_domain(urlsplit(site["url"]).hostname or "", site.get("domain", ""))
        if not domain:
            return None
        row = self.rdap.cached(domain)
        if row is None:
            return {"name": domain, "status": "pending", "days_left": None, "expires": None, "registrar": None, "detail": "not looked up yet"}
        expires = row.get("expires_at")
        return {"name": domain, "status": row["status"], "expires": iso(expires) if expires else None,
                "days_left": int((float(expires) - now) // 86400) if expires else None, "registrar": row.get("registrar") or None,
                "detail": row.get("detail") or None, "checked": iso(row["checked_at"])}

    def status_one(self, site: dict[str, Any], now: Optional[float] = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        st = self.state_of(site["id"])
        enabled = site["enabled"]
        state = st["state"] if enabled else "disabled"
        tls, domain = self._tls_view(st), self._domain_view(site, now)
        warnings: list[str] = []
        if enabled and st["state"] == "down":
            warnings.append(f"down: {st['cause']}")
        elif enabled and st["fail_streak"]:
            warnings.append(f"last check failed ({st['cause']}); confirming")
        if tls and tls.get("days_left") is not None and tls["days_left"] <= CERT_WARN_DAYS[0]:
            warnings.append(f"certificate expires in {tls['days_left']} day(s)")
        if tls and tls.get("ok") is False and tls.get("error"):
            warnings.append(tls["error"])
        if domain and domain.get("days_left") is not None and domain["days_left"] <= DOMAIN_WARN_DAYS[0]:
            warnings.append(f"domain expires in {domain['days_left']} day(s)")
        change = st.get("last_change")
        open_incident = self.incidents.open_for(SITE_PREFIX + site["id"]) if enabled else None
        return {
            "id": site["id"], "name": site["name"], "url": site["url"], "enabled": enabled, "interval_min": site["interval_min"],
            "expect_status": site["expect_status"], "keyword": site["keyword"] or None, "domain_override": site["domain"] or None,
            "state": state, "since": st["since"], "since_iso": iso(st["since"]) if st["since"] else None,
            "for": duration(now - st["since"]) if st["since"] else None,
            "status": st["status"], "latency_ms": st["latency_ms"], "redirect": st["redirect"], "keyword_ok": st["keyword_ok"],
            "cause": st["cause"], "failing_checks": st["fail_streak"] or None,
            "last_check": iso(st["last_check"]) if st["last_check"] else None, "last_check_ts": st["last_check"],
            "next_check": iso(st["next_due"]) if st["next_due"] and enabled else None, "checks": st["checks"],
            "tls": tls, "dns": {"addresses": st["dns"].get("addresses") or [], "changed": iso(st["dns"]["changed_at"]) if st["dns"].get("changed_at") else None,
                                "changes": st["dns"].get("changes") or 0, "error": st["dns"].get("error")},
            "domain": domain,
            "last_change": ({"at": iso(change["at"]), "from": change["from"], "to": change["to"], "cause": change.get("cause")} if change else None),
            "open_incident": open_incident["id"] if open_incident else None, "warnings": warnings,
        }

    def status_all(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        now = self.clock() if now is None else now
        return [self.status_one(site, now) for site in self.store.list()]

    def summary(self) -> dict[str, Any]:
        sites = self.store.list()
        counts: dict[str, int] = {}
        for site in sites:
            state = self.state_of(site["id"])["state"] if site["enabled"] else "disabled"
            counts[state] = counts.get(state, 0) + 1
        return {"total": len(sites), "counts": counts, "running": self.running, "enabled": self.enabled,
                "last_tick": iso(self.last_tick) if self.last_tick else None, "error": self.last_error or self.store.load_error,
                "events_sent": self.sent}

    def lanes(self, since: float, until: float) -> dict[str, dict[str, Any]]:
        """``{site_id: {segments, uptime_pct}}`` for the Panel's small history."""
        ids = [s["id"] for s in self.store.list()]
        services = [SITE_PREFIX + i for i in ids]
        data = views.lanes(self.db, services, since, until, set(services))
        return {i: {"segments": data[SITE_PREFIX + i], "uptime_pct": views.uptime_pct(data[SITE_PREFIX + i], since, until)} for i in ids}

    def history(self, site: dict[str, Any], since: float, until: float, points: int = 48) -> dict[str, Any]:
        service = SITE_PREFIX + site["id"]
        data = views.history(self.db, service, since, until)
        lane = views.lanes(self.db, [service], since, until, {service})[service]
        rows = self.db.query("SELECT ts, latency_ms FROM samples WHERE service = ? AND ts BETWEEN ? AND ? AND state = 'up' AND latency_ms IS NOT NULL ORDER BY ts",
                             (service, since, until))
        values = sorted(r["latency_ms"] for r in rows)
        latency = None
        if values:
            latency = {"min": round(values[0], 1), "avg": round(sum(values) / len(values), 1), "p95": round(values[min(len(values) - 1, int(0.95 * len(values)))], 1),
                       "max": round(values[-1], 1), "samples": len(values)}
        step = max(1.0, (until - since) / max(2, points))
        buckets: dict[int, list[float]] = {}
        for r in rows:
            buckets.setdefault(int((r["ts"] - since) // step), []).append(r["latency_ms"])
        series = [[round(since + i * step), round(sum(v) / len(v), 1), round(max(v), 1)] for i, v in sorted(buckets.items())]
        incidents = []
        for item in self.incidents.list(since, until, service, False, 20):
            end = item.get("closed_at")
            incidents.append({"id": item["id"], "opened": iso(item["opened_at"]), "closed": iso(end) if end else None,
                              "duration": duration((end or until) - item["opened_at"]) + ("" if end else " (still open)"), "cause": item["probable_cause"]})
        return {"site": site["id"], "name": site["name"], "url": site["url"], "since": iso(since), "until": iso(until),
                "state_now": self.state_of(site["id"])["state"], "state_at_since": data["state_at_since"],
                "uptime_pct": views.uptime_pct(lane, since, until), "latency_ms": latency,
                "latency_series_columns": ["ts", "avg_ms", "max_ms"], "latency_series": series,
                "segments": [{"from": iso(a), "to": iso(b), "state": s, "duration": duration(b - a)} for a, b, s in lane][-50:],
                "changes": data["changes"], "truncated": data["truncated"], "incidents": incidents}
