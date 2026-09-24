"""Request guard: host allow-list, Origin rule and Fetch Metadata rules."""

import pytest
from conftest import make_config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cassandra_hoard.guard import check_request, host_of, install_guard, is_allowed_host, parse_allowed_hosts
from cassandra_hoard.main import create_app

NAV = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
CORS = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}
IFRAME = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"}


def test_host_of_strips_scheme_path_port_and_case():
    assert host_of("LocalHost:5183") == "localhost"
    assert host_of("https://My-PC.ts.net:8443/x") == "my-pc.ts.net"
    assert host_of("[::1]:5183") == "[::1]"
    assert host_of("") == "" and host_of(None) == ""


def test_parse_allowed_hosts():
    assert parse_allowed_hosts(" pc.example , *.TS.net,, pc2.example:8443") == ("pc.example", "*.ts.net", "pc2.example")
    assert parse_allowed_hosts(None) == () and parse_allowed_hosts("*.") == ()


def test_is_allowed_host_exact_wildcard_unknown():
    allowed = parse_allowed_hosts("pc.example,*.ts.net")
    for host in ("localhost", "127.0.0.1", "[::1]", "pc.example", "my-pc.ts.net", "a.b.ts.net"):
        assert is_allowed_host(host, allowed), host
    for host in ("ts.net", "evil.example", "pc.example.evil", "", None):
        assert not is_allowed_host(host, allowed), host
    assert not is_allowed_host("my-pc.ts.net", ())


def test_check_request_fetch_metadata_rules():
    local = {"host": "localhost:5190"}
    assert check_request("GET", local) is None  # curl / MCP bridge: no Sec-Fetch headers
    assert check_request("POST", {"host": "127.0.0.1:5190"}) is None
    assert check_request("GET", {**local, **NAV}) is None  # top-level navigation from another site
    assert check_request("GET", {**local, "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"}) is None
    assert check_request("GET", {**local, **CORS})  # cross-site fetch
    assert check_request("GET", {**local, **IFRAME})
    assert check_request("GET", {**local, **NAV, "sec-fetch-dest": "embed"})
    assert check_request("POST", {**local, **NAV})  # form post from another site
    assert check_request("POST", {**local, "sec-fetch-site": "same-origin", "sec-fetch-mode": "navigate"})
    assert check_request("GET", {"host": "evil.example"})


def test_check_request_origin_by_host_not_exact_string():
    allowed = parse_allowed_hosts("*.ts.net")
    headers = lambda origin: {"host": "my-pc.ts.net", "origin": origin}  # noqa: E731
    assert check_request("GET", headers("https://my-pc.ts.net:8443"), allowed) is None
    assert check_request("GET", headers("http://localhost:5183"), allowed) is None
    assert check_request("GET", headers("http://localhost:5173"), allowed) is None  # vite dev
    assert check_request("GET", headers("https://evil.example"), allowed)
    assert check_request("GET", {"host": "localhost", "origin": "http://my-pc.ts.net"})  # not in the list


def test_middleware_navigation_reaches_root_but_not_embeds_or_fetches():
    app = FastAPI()
    install_guard(app, parse_allowed_hosts("*.ts.net"))

    @app.get("/")
    def home():
        return {"ok": True}

    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/", headers=NAV).status_code == 200
        assert client.get("/", headers={**NAV, "host": "my-pc.ts.net"}).status_code == 200
        assert client.get("/", headers=IFRAME).status_code == 403
        assert client.get("/", headers=CORS).status_code == 403
        assert client.get("/", headers={**NAV, "host": "other.example"}).status_code == 403


@pytest.fixture
def guarded(tmp_path):
    app = create_app(make_config(tmp_path, allowed_hosts=parse_allowed_hosts("pc.example, *.ts.net")))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def test_app_host_origin_and_cross_site_rules(guarded):
    get = lambda **headers: guarded.get("/api/health", headers=headers).status_code  # noqa: E731
    # Host rule: local, exact, wildcard; unknown rejected; case and port ignored.
    assert get() == 200
    assert get(host="pc.example") == 200
    assert get(host="My-PC.ts.net:8443") == 200
    assert get(host="evil.example") == 403
    assert get(host="ts.net") == 403
    # Origin rule: allowed host with any scheme/port; anything else 403.
    assert get(origin="https://my-pc.ts.net:8443") == 200
    assert get(origin="http://localhost:5173") == 200
    assert get(origin="https://evil.example") == 403
    # Fetch Metadata: navigation ok, cross-site fetch / iframe / form post rejected.
    assert get(**NAV) == 200
    assert get(**CORS) == 403
    assert get(**IFRAME) == 403
    post = lambda **headers: guarded.post("/api/agent/call", json={}, headers=headers).status_code  # noqa: E731
    assert post(**NAV) == 403
    assert post(**CORS, origin="https://evil.example") == 403
    assert post(**{"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"}) != 403  # reaches the route (wants a token)


def test_app_without_allowed_hosts_is_local_only(client):
    assert client.get("/api/health", headers={"host": "my-pc.ts.net"}).status_code == 403
    assert client.get("/api/health", headers={"host": "[::1]:5183"}).status_code == 200
