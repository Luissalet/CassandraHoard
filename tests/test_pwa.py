"""PWA manifest and service worker: installable on the phone home screen."""


def test_manifest_has_required_fields(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    body = response.json()
    assert body["name"] == "Cassandra's Hoard"
    assert body["short_name"] == "Cassandra"
    assert body["start_url"] == "/"
    assert body["display"] == "standalone"
    assert body["lang"] == "es"
    assert body["background_color"]
    assert body["theme_color"]
    sizes = {icon["sizes"] for icon in body["icons"]}
    assert "192x192" in sizes
    assert "512x512" in sizes


def test_service_worker_is_js_and_installable_at_root(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["service-worker-allowed"] == "/"
    body = response.text
    assert "skipWaiting" in body
    assert "clients.claim" in body
    assert "/api/" in body
    assert "/assets/" in body
