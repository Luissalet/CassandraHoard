"""Public sites: the list, the flapping rule, causes, the schedule and warnings sent once."""

import json

import httpx
import pytest
from conftest import T0, Harness

from cassandra_hoard import sites as S
from cassandra_hoard.sites import SiteStore, SiteWatcher, validate_site


def make(tmp_path, **kwargs):
    h = Harness(tmp_path)
    events: list[tuple[str, dict]] = []
    store = SiteStore(h.config.sites_path)
    watcher = SiteWatcher(h.services.db, store, incidents=h.services.incidents, emit=lambda t, d: events.append((t, d)),
                          clock_fn=h.clock, http_fn=h.sitenet.http_fn, tls_fn=h.sitenet.tls_fn, dns_fn=h.sitenet.dns_fn,
                          rdap_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(599))), **kwargs)
    return h, store, watcher, events


def types(events):
    return [t for t, _ in events]


def rows(h, service, kind=None):
    sql = "SELECT * FROM events WHERE service = ?" + (" AND kind = ?" if kind else "") + " ORDER BY ts, id"
    return h.services.db.query(sql, (service, kind) if kind else (service,))


# -- the list ---------------------------------------------------------------

def test_validation_normalises_and_explains():
    site = validate_site({"url": "Example.com"})
    assert site == {"id": "example.com", "name": "example.com", "url": "https://example.com/", "expect_status": ["2xx", "3xx"],
                    "keyword": "", "interval_min": 5.0, "enabled": True, "domain": ""}
    assert validate_site({"url": "https://www.example.org/shop?x=1", "expect_status": "200, 301-308"})["id"] == "example.org"
    assert validate_site({"url": "http://x.example/", "id": "Blog", "interval_min": 1})["id"] == "blog"
    for bad in ({}, {"url": "ftp://example.com"}, {"url": "https://user:pw@example.com/"}, {"url": "https://example.com:99999/"},
                {"url": "https://example.com/", "interval_min": 0.5}, {"url": "https://example.com/", "id": "Bad Id!"},
                {"url": "https://example.com/", "expect_status": ["teapot"]}):
        with pytest.raises(ValueError):
            validate_site(bad)


def test_status_patterns():
    assert S.status_ok(200, ["2xx", "3xx"]) and S.status_ok(301, ["2xx", "3xx"])
    assert not S.status_ok(404, ["2xx", "3xx"]) and not S.status_ok(503, ["2xx"]) and not S.status_ok(None, ["2xx"])
    assert S.status_ok(404, ["404"]) and S.status_ok(207, ["200-299"]) and not S.status_ok(300, ["200-299"])


def test_the_list_lives_in_the_data_dir_and_persists(tmp_path):
    h = Harness(tmp_path)
    path = h.config.sites_path
    store = SiteStore(path)
    assert store.list() == [] and not path.exists()  # nothing is hard-coded
    site, created = store.upsert({"url": "https://example.com/", "keyword": "Shop"})
    assert created and path.exists() and json.loads(path.read_text(encoding="utf-8"))["sites"][0]["keyword"] == "Shop"
    again = SiteStore(path)  # a new process reads the same list
    assert again.list() == [site] and again.find("EXAMPLE.com")["id"] == "example.com" and again.find("site:example.com")
    edited, created = again.upsert({"id": "example.com", "interval_min": 10, "keyword": ""})
    assert not created and edited["interval_min"] == 10 and edited["keyword"] == "" and edited["url"] == "https://example.com/"
    with pytest.raises(ValueError):
        again.upsert({"id": "other", "url": "https://other.example/", "interval_min": 99999})
    # a hand edit is picked up, a broken entry is skipped and reported, a broken file keeps the last good list
    path.write_text(json.dumps({"sites": [{"url": "https://hand.example/"}, {"url": "nonsense://"}]}), encoding="utf-8")
    assert [s["id"] for s in again.list()] == ["hand.example"] and again.load_error
    path.write_text("{not json", encoding="utf-8")
    assert [s["id"] for s in again.list()] == ["hand.example"] and "cannot be read" in again.load_error
    assert again.remove("hand.example")["id"] == "hand.example" and again.remove("hand.example") is None


# -- the flapping rule and the cause -----------------------------------------

def test_down_after_two_failures_up_on_the_first_success(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/"})
    service = "site:example.com"
    w.check(site)
    assert w.state_of("example.com")["state"] == "up" and events == []  # the first look announces nothing
    h.sitenet.fail("timeout", "Timeout: no answer within 15 s")
    h.clock.advance(300)
    w.check(site)
    st = w.state_of("example.com")
    assert st["state"] == "up" and st["fail_streak"] == 1 and events == []
    assert h.services.incidents.open_for(service) is None
    assert st["next_due"] == h.clock.now + 60  # one failure only asks for a confirming check sooner
    h.clock.advance(60)
    w.check(site)
    assert w.state_of("example.com")["state"] == "down" and types(events) == [S.EV_DOWN]
    incident = h.services.incidents.open_for(service)
    assert incident["kind"] == "site" and incident["probable_cause"].startswith("Timeout") and incident["from_state"] == "up"
    assert events[0][1]["incident_id"] == incident["id"] and events[0][1]["cause_code"] == "timeout" and events[0][1]["site"] == "example.com"
    assert w.state_of("example.com")["next_due"] == h.clock.now + 300
    h.clock.advance(300)
    w.check(site)
    assert types(events) == [S.EV_DOWN] and len(h.services.incidents.list(service=service)) == 1  # still one incident, one alert
    h.sitenet.respond(200, latency=95.0)
    h.clock.advance(300)
    w.check(site)
    assert w.state_of("example.com")["state"] == "up" and types(events) == [S.EV_DOWN, S.EV_UP]
    assert events[1][1]["downtime_s"] == 600.0 and events[1][1]["incident_id"] == incident["id"]
    assert h.services.incidents.open_for(service) is None and h.services.incidents.get(incident["id"])["closed_at"] == h.clock.now
    assert [(r["from_state"], r["to_state"]) for r in rows(h, service, "state")] == [("up", "down"), ("down", "up")]
    states = [r["state"] for r in h.services.db.query("SELECT state FROM samples WHERE service = ? ORDER BY id", (service,))]
    assert states[0] == "up" and "down" in states and states[-1] == "up"


def test_a_single_failure_never_flaps(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/"})
    w.check(site)
    h.sitenet.fail("reset", "Connection reset by the server")
    h.clock.advance(300)
    w.check(site)
    h.sitenet.respond()
    h.clock.advance(60)
    w.check(site)
    st = w.state_of("example.com")
    assert st["state"] == "up" and st["fail_streak"] == 0 and events == [] and h.services.incidents.list(service="site:example.com") == []
    assert rows(h, "site:example.com", "state") == []


@pytest.mark.parametrize("setup, code, text", [
    ("dns", "dns", "DNS failure"),
    ("tls", "tls", "TLS error: certificate has expired"),
    ("timeout", "timeout", "Timeout"),
    ("refused", "refused", "Connection refused"),
    ("503", "http_5xx", "HTTP 503 (server error)"),
    ("404", "http_status", "HTTP 404 (unexpected status)"),
    ("keyword", "keyword", "keyword 'Welcome' is missing"),
])
def test_causes_in_words(tmp_path, setup, code, text):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/", "keyword": "Welcome"})
    w.check(site)
    assert w.state_of("example.com")["keyword_ok"] is True
    calls = h.sitenet.calls["http"]
    if setup == "dns":
        h.sitenet.dns = {"ok": False, "addresses": [], "error": "DNS failure: example.com does not resolve (getaddrinfo failed)"}
    elif setup == "tls":
        h.sitenet.fail("tls", "TLS error: certificate has expired")
    elif setup in ("timeout", "refused"):
        h.sitenet.fail(setup, {"timeout": "Timeout: no answer within 15 s", "refused": "Connection refused: nothing accepts connections on that port"}[setup])
    elif setup == "keyword":
        h.sitenet.respond(200, body="<html>Maintenance</html>")
    else:
        h.sitenet.respond(int(setup), body="Welcome")
    for _ in range(2):
        h.clock.advance(300)
        w.check(site)
    st = w.state_of("example.com")
    assert st["state"] == "down" and st["cause_code"] == code, st["cause"]
    assert text.lower() in h.services.incidents.open_for("site:example.com")["probable_cause"].lower()
    assert types(events) == [S.EV_DOWN] and events[0][1]["cause"] == st["cause"]
    if setup == "dns":
        assert h.sitenet.calls["http"] == calls  # nothing to ask when the name does not resolve
    if setup == "keyword":
        assert st["keyword_ok"] is False


def test_expected_status_is_configurable(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/", "expect_status": ["404"]})
    h.sitenet.respond(404, body="not found")
    w.check(site)
    assert w.state_of("example.com")["state"] == "up"
    h.sitenet.respond(200)
    for _ in range(2):
        h.clock.advance(300)
        w.check(site)
    assert w.state_of("example.com")["state"] == "down" and "HTTP 200" in w.state_of("example.com")["cause"]


def test_cause_change_while_down_is_recorded_not_announced(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/"})
    w.check(site)
    h.sitenet.fail("timeout", "Timeout: no answer within 15 s")
    for _ in range(2):
        h.clock.advance(300)
        w.check(site)
    h.sitenet.respond(502)
    h.clock.advance(300)
    w.check(site)
    incident = h.services.incidents.open_for("site:example.com")
    assert types(events) == [S.EV_DOWN] and [a["kind"] for a in incident["actions"]] == ["cause_changed"]
    assert "502" in incident["actions"][0]["detail"]


# -- the schedule -----------------------------------------------------------

def test_the_interval_is_respected(tmp_path):
    h, store, w, events = make(tmp_path)
    store.upsert({"url": "https://example.com/", "interval_min": 5})
    store.upsert({"url": "https://paused.example/", "enabled": False})
    assert w.tick() == ["example.com"] and h.sitenet.calls["http"] == 1
    h.clock.advance(299)
    assert w.tick() == [] and h.sitenet.calls["http"] == 1
    h.clock.advance(2)
    assert w.tick() == ["example.com"] and h.sitenet.calls["http"] == 2
    assert all("paused" not in u for u in h.sitenet.urls)
    # a restart does not ask again before the stored time
    again = SiteWatcher(h.services.db, store, incidents=h.services.incidents, emit=lambda *a: None, clock_fn=h.clock,
                        http_fn=h.sitenet.http_fn, tls_fn=h.sitenet.tls_fn, dns_fn=h.sitenet.dns_fn)
    assert again.state_of("example.com")["state"] == "up" and again.tick() == [] and h.sitenet.calls["http"] == 2


def test_manual_check_is_rate_limited(tmp_path):
    h, store, w, events = make(tmp_path)
    store.upsert({"url": "https://example.com/"})
    assert w.check_now() == [{"id": "example.com", "checked": True}]
    out = w.check_now("example.com")
    assert out[0]["checked"] is False and "once a minute" in out[0]["reason"] and h.sitenet.calls["http"] == 1
    h.clock.advance(61)
    assert w.check_now("example.com")[0]["checked"] is True
    with pytest.raises(LookupError):
        w.check_now("nope")


def test_the_request_is_a_polite_single_get(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/shop"})
    w.check(site)
    assert h.sitenet.urls == ["https://example.com/shop"] and h.sitenet.calls == {"http": 1, "tls": 1, "dns": 1}
    h.clock.advance(300)
    w.check(site)
    assert h.sitenet.calls == {"http": 2, "tls": 1, "dns": 2}  # the handshake is repeated hourly, not every check
    h.clock.advance(3600)
    w.check(site)
    assert h.sitenet.calls["tls"] == 2


# -- certificate warnings, once per certificate -------------------------------

def set_cert(h, days, fingerprint="fp-one"):
    h.sitenet.tls = {**h.sitenet.tls, "days_left": days, "not_after": h.clock.now + days * 86400, "fingerprint": fingerprint}


def test_certificate_warnings_are_sent_once_per_certificate(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/"})

    def step(days, fingerprint="fp-one"):
        h.clock.advance(3700)
        set_cert(h, days, fingerprint)
        w.check(site)

    step(30)
    assert events == []
    step(20)
    assert [(d["threshold"], d["days_left"]) for t, d in events if t == S.EV_CERT] == [(21, 20)]
    step(19)
    step(18)
    assert len(events) == 1  # the same threshold is not repeated
    step(6)
    step(5)
    assert [d["threshold"] for t, d in events] == [21, 7]
    step(0)
    assert [d["threshold"] for t, d in events] == [21, 7, 1]
    assert events[0][1]["issuer"] == "Example CA (E1)" and events[0][1]["site"] == "example.com" and events[0][1]["not_after"]
    step(80, "fp-two")  # renewed
    assert len(events) == 3 and any("certificate changed" in r["detail"] for r in rows(h, "site:example.com", "cert"))
    step(20, "fp-two")  # the new certificate gets its own warnings
    assert [d["threshold"] for t, d in events] == [21, 7, 1, 21]
    # the stored state survives a restart: the same certificate does not warn again
    again = SiteWatcher(h.services.db, store, incidents=h.services.incidents, emit=lambda t, d: events.append((t, d)), clock_fn=h.clock,
                        http_fn=h.sitenet.http_fn, tls_fn=h.sitenet.tls_fn, dns_fn=h.sitenet.dns_fn)
    h.clock.advance(3700)
    set_cert(h, 19, "fp-two")
    again.check(site)
    assert len(events) == 4


def test_an_expired_certificate_is_reported_with_the_days(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/"})
    h.sitenet.tls = {**h.sitenet.tls, "ok": False, "days_left": -3, "not_after": T0 - 3 * 86400, "chain_valid": False,
                     "error": "TLS error: certificate has expired"}
    w.check(site)
    status = w.status_one(site)
    assert events[0][0] == S.EV_CERT and events[0][1]["days_left"] == -3
    assert status["tls"]["error"] == "TLS error: certificate has expired" and any("TLS error" in x for x in status["warnings"])
    assert "expired 3 day(s) ago" in rows(h, "site:example.com", "cert")[0]["detail"]


def test_a_plain_http_site_has_no_tls_part(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "http://plain.example/"})
    w.check(site)
    assert h.sitenet.calls["tls"] == 0 and w.status_one(site)["tls"] is None


# -- DNS ----------------------------------------------------------------------

def test_dns_answers_are_recorded_and_changes_flagged(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/"})
    w.check(site)
    assert w.status_one(site)["dns"] == {"addresses": ["203.0.113.10"], "changed": None, "changes": 0, "error": None}
    h.sitenet.dns = {"ok": True, "addresses": ["203.0.113.10", "2001:db8::1"], "error": None}
    h.clock.advance(300)
    w.check(site)
    dns = w.status_one(site)["dns"]
    assert dns["addresses"] == ["203.0.113.10", "2001:db8::1"] and dns["changes"] == 1 and dns["changed"]
    assert "203.0.113.10 → 203.0.113.10, 2001:db8::1" in rows(h, "site:example.com", "dns")[0]["detail"]
    assert events == [] and h.services.incidents.list(service="site:example.com") == []  # a CDN rotating addresses is not an incident


# -- views --------------------------------------------------------------------

def test_status_history_and_lanes(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/", "name": "Shop"})
    w.check(site)
    h.sitenet.fail("timeout", "Timeout: no answer within 15 s")
    for _ in range(2):
        h.clock.advance(300)
        w.check(site)
    h.sitenet.respond(latency=80.0)
    h.clock.advance(300)
    w.check(site)
    status = w.status_one(site)
    assert status["state"] == "up" and status["name"] == "Shop" and status["status"] == 200 and status["latency_ms"] == 80.0
    assert status["last_change"]["from"] == "down" and status["last_change"]["to"] == "up"
    assert status["tls"]["days_left"] == 80 and status["domain"]["name"] == "example.com" and status["domain"]["status"] == "error"
    assert w.summary()["counts"] == {"up": 1} and w.name_of("site:example.com") == "Shop"
    assert h.services.name_of("site:example.com") == "Shop"  # the service's own watcher reads the same list
    history = w.history(site, T0 - 60, h.clock.now + 60)
    assert history["state_now"] == "up" and [c["to"] for c in history["changes"] if c["kind"] == "state"] == ["down", "up"]
    assert history["incidents"][0]["cause"].startswith("Timeout") and history["latency_ms"]["max"] == 120.0
    assert history["uptime_pct"] < 100 and {s["state"] for s in history["segments"]} == {"up", "down"}
    lanes = w.lanes(T0 - 60, h.clock.now + 60)
    assert [seg[2] for seg in lanes["example.com"]["segments"]] == ["up", "down", "up"]


def test_removing_a_site_closes_its_incident_and_keeps_the_history(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/"})
    w.check(site)
    h.sitenet.fail("timeout", "Timeout: no answer within 15 s")
    for _ in range(2):
        h.clock.advance(300)
        w.check(site)
    assert h.services.incidents.open_for("site:example.com")
    removed = store.remove("example.com")
    w.forget(removed)
    assert h.services.incidents.open_for("site:example.com") is None and w.state_of("example.com")["state"] == "unknown"
    assert len(h.services.incidents.list(service="site:example.com")) == 1 and rows(h, "site:example.com", "state")


def test_site_events_are_not_blamed_on_a_service_incident(tmp_path):
    h, store, w, events = make(tmp_path)
    site, _ = store.upsert({"url": "https://example.com/"})
    w.check(site)
    h.sitenet.fail("timeout", "Timeout: no answer within 15 s")
    for _ in range(2):
        h.clock.advance(5)
        w.check(site)
    ctx = h.services.incidents.capture({"opened_at": h.clock.now, "service": "argus", "kind": "down"}, {})
    assert rows(h, "site:example.com", "state") and ctx["correlated"] == []
