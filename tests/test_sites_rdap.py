"""Domain registration expiry through RDAP (mocked): bootstrap, fallback, cache, absence, and the 30/7/1 warnings."""

from datetime import datetime, timezone

import httpx
from conftest import T0, Harness

from cassandra_hoard import sites as S
from cassandra_hoard import sites_net as N

BOOT = {"services": [[["com", "net"], ["http://rdap.registry.example/com/", "https://rdap.registry.example/com/"]],
                     [["org"], ["https://rdap.other.example/"]]]}


def iso_at(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class Registry:
    """Answers the bootstrap, the TLD's RDAP server and the rdap.org fallback; records every request."""

    def __init__(self, expires=T0 + 100 * 86400):
        self.expires = expires
        self.log: list[str] = []
        self.registry_status = 200
        self.fallback_status = 200
        self.events = None  # None = a normal expiration event

    def entity(self):
        return [{"roles": ["registrar"], "vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["fn", {}, "text", "Example Registrar Inc."]]]}]

    def body(self):
        events = self.events if self.events is not None else [{"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"},
                                                               {"eventAction": "expiration", "eventDate": iso_at(self.expires)}]
        return {"ldhName": "example.com", "events": events, "entities": self.entity()}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.log.append(str(request.url))
        host = request.url.host
        if host == "data.iana.org":
            return httpx.Response(200, json=BOOT)
        if host == "rdap.registry.example":
            return httpx.Response(self.registry_status, json=self.body() if self.registry_status == 200 else {})
        if host == "rdap.org":
            return httpx.Response(self.fallback_status, json=self.body() if self.fallback_status == 200 else {})
        raise httpx.ConnectError("unreachable", request=request)


def checker(h, registry):
    return N.RdapChecker(h.services.db, client=httpx.Client(transport=httpx.MockTransport(registry)), clock_fn=h.clock)


def test_registrable_domain():
    assert N.registrable_domain("www.example.com") == "example.com"
    assert N.registrable_domain("shop.eu.example.co.uk") == "example.co.uk"
    assert N.registrable_domain("example.com", "Other.org.") == "other.org"
    assert N.registrable_domain("203.0.113.5") is None and N.registrable_domain("localhost") is None and N.registrable_domain("") is None


def test_lookup_through_the_bootstrap_and_the_daily_cache(tmp_path):
    h = Harness(tmp_path)
    reg = Registry()
    rdap = checker(h, reg)
    row = rdap.get("example.com")
    assert row["status"] == "ok" and row["expires_at"] == T0 + 100 * 86400 and row["registrar"] == "Example Registrar Inc."
    assert reg.log == ["https://data.iana.org/rdap/dns.json", "https://rdap.registry.example/com/domain/example.com"]  # https server preferred
    h.clock.advance(3600)
    assert rdap.get("example.com")["status"] == "ok" and len(reg.log) == 2  # cached: once a day
    h.clock.advance(86400)
    rdap.get("example.com")
    assert reg.log[2:] == ["https://rdap.registry.example/com/domain/example.com"]  # the bootstrap is cached for a week
    h.clock.advance(8 * 86400)
    rdap.get("example.com")
    assert reg.log[-2] == "https://data.iana.org/rdap/dns.json"


def test_absence_is_unknown_not_an_error(tmp_path):
    h = Harness(tmp_path)
    reg = Registry()
    rdap = checker(h, reg)
    reg.events = [{"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"}]
    row = rdap.get("example.com")
    assert row["status"] == "unknown" and "no expiration date" in row["detail"] and row["expires_at"] is None
    reg.registry_status = 404
    h.clock.advance(86400 + 1)
    assert rdap.get("example.com")["status"] == "unknown"
    # a TLD with no RDAP service at all: the fallback says 404
    reg.fallback_status = 404
    row = rdap.get("example.xx")
    assert row["status"] == "unknown" and "no RDAP service known for .xx" in row["detail"]


def test_falls_back_to_rdap_org_and_retries_errors_after_six_hours(tmp_path):
    h = Harness(tmp_path)
    reg = Registry()
    rdap = checker(h, reg)
    reg.registry_status = 503
    row = rdap.get("example.com")
    assert row["status"] == "ok" and reg.log[-1] == "https://rdap.org/domain/example.com"
    reg.fallback_status = 500
    h.clock.advance(86400 + 1)
    assert rdap.get("example.com")["status"] == "error"
    count = len(reg.log)
    h.clock.advance(3600)
    assert rdap.get("example.com")["status"] == "error" and len(reg.log) == count  # no hammering after a failure
    reg.fallback_status = 200
    h.clock.advance(6 * 3600)
    assert rdap.get("example.com")["status"] == "ok"


def test_a_dead_network_is_an_answer_not_an_exception(tmp_path):
    h = Harness(tmp_path)

    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    rdap = N.RdapChecker(h.services.db, client=httpx.Client(transport=httpx.MockTransport(boom)), clock_fn=h.clock)
    row = rdap.get("example.com")
    assert row["status"] == "error" and "RDAP" in row["detail"]


def test_domain_warnings_once_per_expiry_for_all_the_sites_sharing_it(tmp_path):
    h = Harness(tmp_path)
    reg = Registry(expires=T0 + 40 * 86400)
    events = []
    store = S.SiteStore(h.config.sites_path)
    a, _ = store.upsert({"url": "https://shop.example.com/"})
    b, _ = store.upsert({"url": "https://www.example.com/"})
    w = S.SiteWatcher(h.services.db, store, incidents=h.services.incidents, emit=lambda t, d: events.append((t, d)), clock_fn=h.clock,
                      http_fn=h.sitenet.http_fn, tls_fn=h.sitenet.tls_fn, dns_fn=h.sitenet.dns_fn, rdap=checker(h, reg))

    def day(days_left, expires=None):
        h.clock.advance(86400 + 5)
        reg.expires = expires or (h.clock.now + days_left * 86400 + 600)
        w.check(a)
        w.check(b)

    day(40)
    assert events == []
    day(25)
    assert [(t, d["threshold"], d["days_left"]) for t, d in events] == [(S.EV_DOMAIN, 30, 25)]
    assert events[0][1]["domain"] == "example.com" and sorted(events[0][1]["sites"]) == ["example.com", "shop.example.com"]
    day(24)
    assert len(events) == 1  # once, not once per site and not once per day
    day(5)
    day(0)
    assert [d["threshold"] for t, d in events] == [30, 7, 1]
    day(400)  # renewed for another year
    assert len(events) == 3
    day(18)  # the next expiry date warns again
    assert [d["threshold"] for t, d in events] == [30, 7, 1, 30]
    status = w.status_one(a)
    assert status["domain"]["name"] == "example.com" and status["domain"]["days_left"] == 18 and status["domain"]["registrar"] == "Example Registrar Inc."
    assert any("domain expires in 18" in x for x in status["warnings"])
    assert any(r["kind"] == "domain" for r in h.services.db.query("SELECT kind FROM events WHERE service = 'site:shop.example.com'"))


def test_unknown_domain_expiry_never_warns(tmp_path):
    h = Harness(tmp_path)
    reg = Registry()
    reg.events = []
    events = []
    store = S.SiteStore(h.config.sites_path)
    a, _ = store.upsert({"url": "https://example.com/"})
    w = S.SiteWatcher(h.services.db, store, incidents=h.services.incidents, emit=lambda t, d: events.append((t, d)), clock_fn=h.clock,
                      http_fn=h.sitenet.http_fn, tls_fn=h.sitenet.tls_fn, dns_fn=h.sitenet.dns_fn, rdap=checker(h, reg))
    w.check(a)
    domain = w.status_one(a)["domain"]
    assert domain["status"] == "unknown" and domain["days_left"] is None and domain["detail"] and events == []
