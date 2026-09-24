"""faustus-plugin.json stays in sync with the code."""

import json
from pathlib import Path

from cassandra_hoard import SERVICE
from cassandra_hoard.agent_tools import TOOLS
from cassandra_hoard.config import DEFAULT_PORT

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_matches_code():
    manifest = json.loads((ROOT / "faustus-plugin.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "cassandra" and manifest["name"] == "Cassandra's Hoard"
    assert manifest["app"]["health"]["expect"]["service"] == SERVICE
    assert manifest["app"]["url_default"].endswith(f":{DEFAULT_PORT}")
    assert manifest["app"]["launch_hint"]["env"]["PORT_STRICT"] == "1"
    # The host refuses unknown top-level keys, so the tool names live in the notes/README, not the manifest.
    for tool in TOOLS:
        assert tool.name in (ROOT / "README.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8") + (ROOT / "README.es.md").read_text(encoding="utf-8")
    for tool in TOOLS:
        assert f"`{tool.name}`" in readme
