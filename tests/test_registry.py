"""Registry: discovery of faustus-plugin.json, built-in externals, user services and policies."""

import json
import sys

import pytest
from conftest import make_config, write_manifest

from cassandra_hoard.registry import Registry, builtin_externals, read_manifest, resolve_placeholders, validate_user_entry


def test_discovery_like_the_launcher(tmp_path):
    root = tmp_path / "apps"
    argus = write_manifest(root, "Argus's Hoard", "argus", 5183)
    write_manifest(root, "Hub", "hoardhub", 8810, service="hoard-hub", name="Hoard Hub")
    write_manifest(root, "Self", "cassandra", 5190)  # itself: never watched
    (root / "Broken").mkdir()
    (root / "Broken" / "faustus-plugin.json").write_text("{nope", encoding="utf-8")
    (argus / "data").mkdir()
    (argus / "data" / "url").write_text("http://127.0.0.1:5199\n", encoding="utf-8")  # moved port wins
    registry = Registry(make_config(tmp_path, externals=True))
    ids = [s.id for s in registry.list()]
    assert "cassandra" not in ids and "argus" in ids
    assert ids.count("hoardhub") == 1 and registry.get("hoardhub").kind == "app"  # the manifest replaces the built-in
    a = registry.get("argus")
    assert a.url == "http://127.0.0.1:5199" and a.expect == {"service": "argus-hoard"}
    assert a.launch.executable == sys.executable and a.launch.cwd == str(argus) and a.launch.argv == ["-m", "argus"]
    assert a.log_globs[0].endswith("data/logs/*.log".replace("/", __import__("os").sep))
    assert {"faustus", "faustus-7001", "llama-server", "llama-helper", "ollama", "comfyui", "comfyui-8191"} <= set(ids)
    assert registry.get("ollama").health_path == "/api/tags" and registry.get("comfyui").health_path == "/system_stats"
    assert ids.index("faustus") < ids.index("ollama") < ids.index("comfyui") < ids.index("hoardhub") < ids.index("argus")
    assert registry.find("Argus") is a and registry.find("5199") is a and registry.find("argus's hoard") is a


def test_externals_off_and_placeholders(tmp_path):
    registry = Registry(make_config(tmp_path))
    assert registry.list() == []
    missing = set()
    out = resolve_placeholders("{PYTHON} {X_DIR} {NOPE}", folder="/f", defaults={"PYTHON": "{FAUSTUS_PYTHON}"}, extra={"FAUSTUS_PYTHON": "/py"}, missing=missing)
    assert out == "/py /f {NOPE}" and missing == {"NOPE"}
    assert len(builtin_externals()) == 12  # 4 Faustus + 2 llama-server + Ollama + 4 ComfyUI + hub


def test_manifest_with_unresolvable_launch(tmp_path):
    folder = tmp_path / "x"
    folder.mkdir()
    (folder / "faustus-plugin.json").write_text(json.dumps({
        "id": "x", "app": {"url_default": "http://127.0.0.1:6000", "launch_hint": {"executable": "{WHO}", "argv": []}}}), encoding="utf-8")
    service = read_manifest(folder / "faustus-plugin.json")
    assert service.launch is None and "unresolved placeholders: WHO" in service.launch_reason
    assert service.health_path == "/api/health" and service.expect == {}


def test_user_services_merge_and_persist(tmp_path):
    root = tmp_path / "apps"
    write_manifest(root, "Borges", "borges", 5184)
    config = make_config(tmp_path, externals=True)
    registry = Registry(config)
    custom = registry.upsert_user({"id": "whisper", "name": "Whisper server", "url": "http://127.0.0.1:9000/", "health_path": "health",
                                   "log_paths": ["C:/logs/whisper*.log"], "restart": {"enabled": True, "cmd": "run.bat", "max_per_hour": 5}})
    assert custom.kind == "user" and custom.url == "http://127.0.0.1:9000" and custom.health_path == "/health" and custom.group == "custom"
    assert custom.restart.enabled and custom.restart.cmd == "run.bat" and custom.restart.max_per_hour == 5
    edited = registry.upsert_user({"id": "borges", "log_paths": ["/var/log/borges.log"]})  # partial edit of a discovered app
    assert edited.kind == "app" and edited.log_globs[-1] == "/var/log/borges.log" and edited.launch is not None
    registry.set_policy("ollama", {"enabled": True, "cmd": ["ollama", "serve"]})
    again = Registry(config)
    assert again.get("whisper").restart.cmd == "run.bat"
    assert again.get("ollama").restart.cmd == ["ollama", "serve"] and again.get("ollama").restart.enabled
    assert again.get("borges").log_globs[-1] == "/var/log/borges.log"
    saved = json.loads(config.services_path.read_text(encoding="utf-8"))
    assert [e["id"] for e in saved["services"]] == ["whisper", "borges"] and "ollama" in saved["policies"]
    assert again.remove_user("whisper") and again.get("whisper") is None and not again.remove_user("whisper")
    with pytest.raises(ValueError):
        again.upsert_user({"id": "brand-new"})  # a new service needs a url


def test_validation_and_broken_file(tmp_path):
    for bad in ({"id": "Bad Id"}, {"id": "system"}, {"id": "ok", "url": "ftp://x"}, {"id": "ok", "expect": [1]}):
        with pytest.raises(ValueError):
            validate_user_entry(bad)
    config = make_config(tmp_path)
    config.data_dir.mkdir(parents=True)
    config.services_path.write_text("[{\"id\": \"a\", \"url\": \"http://127.0.0.1:1\"}, {\"id\": \"b a d\"}]", encoding="utf-8")
    registry = Registry(config)
    assert [s.id for s in registry.list()] == ["a"] and "b a d" in registry.load_error
    config.services_path.write_text("{broken", encoding="utf-8")
    assert Registry(config).load_error.startswith("services.json")
