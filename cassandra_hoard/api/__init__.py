"""API routers."""

from .agent import router as agent_router
from .farm import router as farm_router
from .history import router as history_router
from .settings import router as settings_router
from .sites import router as sites_router
from .status import router as status_router

ROUTERS = [status_router, history_router, settings_router, sites_router, agent_router, farm_router]
