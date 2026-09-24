import json
import sys
import warnings
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse  # noqa: E402

from cassandra_hoard.config import Config  # noqa: E402
from cassandra_hoard.gpu import GpuSample  # noqa: E402
from cassandra_hoard.main import create_app  # noqa: E402
from cassandra_hoard.services import Services  # noqa: E402

T0 = 1_790_000_000.0  # a fixed "now" for deterministic tests


class FakeClock:
    def __init__(self, start: float = T0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class FakeService:
    """An ASGI app that flips between up, down (raises: nothing answers), degraded (503) and foreign."""

    def __init__(self, service: str):
        self.service = service
        self.mode = "up"
        app = FastAPI()

        @app.get("/{path:path}")
        def health(path: str):
            if self.mode == "down":
                raise RuntimeError("connection refused")
            if self.mode == "degraded":
                return JSONResponse({"service": self.service}, status_code=503)
            if self.mode == "foreign":
                return PlainTextResponse("<html>something else</html>")
            return {"service": self.service, "status": "ok", "models": [], "system": {}}

        self.app = app


class FakeNet(httpx.AsyncBaseTransport):
    """Routes http://127.0.0.1:<port> to FakeService apps; unknown ports refuse like a closed port."""

    def __init__(self):
        self.apps: dict[int, FakeService] = {}

    def add(self, port: int, service: str) -> FakeService:
        fake = FakeService(service)
        self.apps[port] = fake
        return fake

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        fake = self.apps.get(request.url.port)
        if fake is None:
            raise httpx.ConnectError("refused", request=request)
        transport = httpx.ASGITransport(app=fake.app)
        return await transport.handle_async_request(request)

    def listeners(self) -> dict[int, int]:
        """What psutil would say: every port whose app is not down listens, pid = 1000 + port."""
        return {port: 1000 + port for port, fake in self.apps.items() if fake.mode != "down"}


class FakeGpu:
    def __init__(self):
        self.samples = [GpuSample(0, 2000, 24000, 5.0), GpuSample(1, 1000, 12000, 0.0)]

    def __call__(self):
        return list(self.samples)


def write_manifest(root: Path, folder: str, app_id: str, port: int, service: str | None = None, name: str | None = None) -> Path:
    app_dir = root / folder
    app_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 1, "id": app_id, "name": name or f"{app_id.title()}'s Hoard",
        "defaults": {"APP_URL": f"http://127.0.0.1:{port}", "PYTHON": "{FAUSTUS_PYTHON}"},
        "app": {
            "url_default": f"http://127.0.0.1:{port}",
            "health": {"path": "/api/health", "expect": {"service": service or f"{app_id}-hoard"}},
            "launch_hint": {"kind": "process", "executable": "{PYTHON}", "argv": ["-m", app_id], "cwd": "{" + app_id.upper() + "_DIR}"},
        },
    }
    (app_dir / "faustus-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return app_dir


def make_config(tmp_path: Path, **overrides) -> Config:
    base = dict(data_dir=tmp_path / "data", roots=[str(tmp_path / "apps")], externals=False, gpu=False, autostart=False,
                default_logs=False, data_dir_configured=True, poll_s=20.0, sample_every_s=60.0)
    base.update(overrides)
    return Config(**base)


class Harness:
    """Services with a fake network, clock, GPU and process table — ticks are explicit."""

    def __init__(self, tmp_path: Path, apps: dict[str, int] | None = None, **config_overrides):
        self.tmp = tmp_path
        self.apps_root = tmp_path / "apps"
        self.apps_root.mkdir(parents=True, exist_ok=True)
        self.net = FakeNet()
        self.clock = FakeClock()
        self.gpu = FakeGpu()
        self.alive: dict[int, bool] = {}
        self.boot = T0 - 3600
        self.spawned: list[tuple] = []
        for app_id, port in (apps or {}).items():
            write_manifest(self.apps_root, f"{app_id.title()}Hoard", app_id, port)
            self.net.add(port, f"{app_id}-hoard")
        overrides = {"gpu": True, **config_overrides}
        self.config = make_config(tmp_path, **overrides)

        def spawn(cmd, cwd, log_path, env=None):
            self.spawned.append((cmd, cwd, log_path))

            class Child:
                pid = 4242

            return Child()

        self.services = Services(
            self.config, clock_fn=self.clock,
            poller_kwargs=dict(transport=self.net, listeners_fn=self.net.listeners, proc_fn=self.proc,
                               alive_fn=lambda pid, started=None: self.alive.get(pid), boot_fn=lambda: self.boot,
                               gpu_reader=self.gpu, restart_async=False),
            restarter_kwargs=dict(hub_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(599))), spawn=spawn),
        )

    def proc(self, pid):
        from cassandra_hoard.procs import ProcInfo

        return ProcInfo(pid, name="python.exe", cmdline=f"python -m app{pid}", started=T0 - 100 - pid)

    def tick(self, advance: float = 20.0):
        self.clock.advance(advance)
        return self.services.poller.tick()

    def close(self):
        self.services.stop()


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path, {"argus": 5183, "borges": 5184, "scribe": 5185})
    yield h
    h.close()


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    h = Harness(tmp_path, {"argus": 5183, "borges": 5184})
    app = create_app(h.config, h.services)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.h = h
        yield test_client
