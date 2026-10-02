"""FastAPI application factory: request guard, API routers, static SPA."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from . import SERVICE, __version__
from .agenda import make_provider
from .api import ROUTERS
from .config import Config
from .hoard_link import fam_agenda, family
from .hoard_link.guard import install_guard
from .hoard_link.service import health_router, install_error_handlers, install_pwa, install_spa
from .services import Services

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(config: Config | None = None, services: Services | None = None) -> FastAPI:
    """``services`` lets tests inject a pre-built instance (fake network and clock)."""
    config = config or (services.config if services else Config.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or Services(config)
        app.state.services = svc
        svc.start()
        logging.getLogger("cassandra").info("Cassandra's Hoard %s — data in %s", __version__, config.data_dir)
        try:
            yield
        finally:
            svc.stop()

    app = FastAPI(title="Cassandra's Hoard", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.config = config
    # Hoard Link 0.4: this app on the family bus (agent.call events, calls to
    # siblings through the hub, the hoard_link block in /api/health).
    family.configure("cassandra", str(config.data_dir), token_file=str(config.token_path))

    install_guard(app, port_getter=lambda: config.port, allowed_env="CASSANDRA_ALLOWED_HOSTS", allowed_hosts=config.allowed_hosts)
    install_error_handlers(app)

    app.include_router(health_router(SERVICE, __version__, extra=lambda: {"dataDirConfigured": config.data_dir_configured}))
    for router in ROUTERS:
        app.include_router(router)

    # the family agenda (open incidents): the hub asks with this app's bearer token
    fam_agenda.install_fastapi(app, make_provider(lambda: getattr(app.state, "services", None), lambda: f"http://127.0.0.1:{config.port}"))

    install_pwa(app, name="Cassandra's Hoard", short_name="Cassandra", theme="#a3245f", background="#010b1b", cache="cassandra-hoard-assets",
                lang="es", static_dir=STATIC_DIR, version=__version__)
    install_spa(app, STATIC_DIR)  # last: everything that is not an API route or a real file is the single page app
    return app
