"""Stdio MCP bridge for Cassandra's Hoard.

It never opens the database: every tool call is proxied to the running app (`POST /api/agent/call`) with the
Bearer token from `<DATA_DIR>/mcp-token`. The tool list comes from `GET /api/agent/tools` (refreshed while the
bridge runs), so the bridge and the app can never disagree. When nothing answers, the bridge starts the app
itself (`python -m cassandra_hoard`, detached, on the port of CASSANDRA_URL) and waits for it;
CASSANDRA_BRIDGE_AUTOSTART=0 turns that off. The bridge itself is the shared catalogue bridge of Hoard Link.
"""

from __future__ import annotations

import sys

from cassandra_hoard.hoard_link.bridge import CatalogBridge


def main() -> int:
    CatalogBridge(app="cassandra", service="cassandra-hoard", package="cassandra_hoard", default_port=5190,
                  data_dir_env="CASSANDRA_DATA_DIR", title="Cassandra's Hoard", root=__file__).run_bridge()
    return 0


if __name__ == "__main__":
    sys.exit(main())
