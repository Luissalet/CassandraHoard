"""API routers."""

from .agent import router as agent_router
from .history import router as history_router
from .pwa import router as pwa_router
from .settings import router as settings_router
from .status import router as status_router

ROUTERS = [status_router, history_router, settings_router, pwa_router, agent_router]
