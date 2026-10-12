"""The family assistant panel: relay routes, launcher injection, manifest entry and the client contract stay in place."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_copilot_info_answers_and_names_the_app(client):
    response = client.get("/api/copilot/info")
    assert response.status_code in (200, 503)  # 503 while the assistant service cannot be reached
    if response.status_code == 503:
        return
    body = response.json()
    assert body["app_id"] == "cassandra"
    assert body["app_name"]
    assert body["panel_url"] == "/api/copilot/panel.js"
    assert body["data_space_id"]


def test_html_carries_the_launcher_and_the_routes_exist(client):
    page = client.get("/")
    assert page.status_code == 200
    assert '<script type="module" src="/api/copilot/launcher.js">' in page.text
    launcher = client.get("/api/copilot/launcher.js")
    assert launcher.status_code == 200 and "hoardCopilot" in launcher.text
    panel = client.get("/api/copilot/panel.js")
    # the script comes from the assistant service: served when it is up, a clean 5xx (never a 404) when it is not
    assert panel.status_code in (200, 502, 503)
    if panel.status_code == 200:
        assert "javascript" in panel.headers["content-type"]


def test_deep_links_also_get_the_launcher(client):
    page = client.get("/incidents/1")
    assert page.status_code == 200 and "/api/copilot/launcher.js" in page.text


def test_manifest_declares_the_copilot_contract_and_keeps_the_agenda_flag():
    manifest = json.loads((ROOT / "faustus-plugin.json").read_text(encoding="utf-8"))
    family = manifest["x-family"]
    assert family["agenda"] is True
    copilot = family["copilot"]
    assert copilot["provider"] == "faustus" and copilot["contract"] == "1.0"
    assert copilot["panel"] == "/api/copilot/panel.js"
    assert {"service", "site", "incident", "gpu", "filters", "editor"} <= set(copilot["context"])


def test_client_registers_the_contract_once_from_the_entry():
    main = (ROOT / "client" / "src" / "main.jsx").read_text(encoding="utf-8")
    assert "installCopilot()" in main
    module = (ROOT / "client" / "src" / "copilot-context.js").read_text(encoding="utf-8")
    assert re.search(r"hoardCopilot\s*=\s*\{\s*context,\s*onTurnEnd\s*\}", module)
    assert "hoard:copilot-turn-end" in module


def test_site_edits_are_patches(client, monkeypatch):
    """The sites form sends only the fields the user changed, so this must keep merging into the stored site."""
    from cassandra_hoard.hoard_link import family

    monkeypatch.setattr(family, "emit", lambda *args, **kwargs: True)  # never the real family bus
    created = client.post("/api/sites", json={"url": "https://example.org/", "name": "Example", "keyword": "hello", "interval_min": 5})
    assert created.status_code == 201
    site_id = created.json()["site"]["id"]
    edited = client.post("/api/sites", json={"id": site_id, "interval_min": 15})
    assert edited.status_code == 201
    site = edited.json()["site"]
    assert site["interval_min"] == 15 and site["keyword"] == "hello" and site["name"] == "Example"


def test_docs_list_the_copilot_routes():
    for name in ("README.md", "README.es.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for route in ("/api/copilot/info", "/api/copilot/panel.js", "/api/copilot/proxy", "/api/copilot/launcher.js", "window.hoardCopilot.context()", "HOARD_COPILOT=0"):
            assert route in text, (name, route)
