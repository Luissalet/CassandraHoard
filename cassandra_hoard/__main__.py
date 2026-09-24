"""`python -m cassandra_hoard` — run the app with uvicorn on 127.0.0.1."""

from __future__ import annotations

import logging

import uvicorn

from .config import Config
from .main import create_app
from .port import find_available_port


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = Config.from_env()
    port = config.port if config.port_strict else find_available_port(config.port)
    config.port = port
    app = create_app(config)
    print(f"Cassandra's Hoard listening on http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
