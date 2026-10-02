"""`python -m cassandra_hoard` — run the app with uvicorn on 127.0.0.1."""

from __future__ import annotations

from .hoard_link.service import run_main


def main() -> int:
    return run_main(service="cassandra-hoard", package="cassandra_hoard", default_port=5190,
                    app_factory="cassandra_hoard.main:create_app", data_dir_env="CASSANDRA_DATA_DIR", port_env="CASSANDRA_PORT",
                    open_browser_default=False, title="Cassandra's Hoard")


if __name__ == "__main__":
    raise SystemExit(main())
