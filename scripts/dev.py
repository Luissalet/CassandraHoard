"""Development: uvicorn --reload (API on CASSANDRA_PORT) + vite dev server (proxying /api)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cassandra_hoard.config import Config  # noqa: E402


def main() -> int:
    config = Config.from_env()
    env = {**os.environ, "CASSANDRA_PORT": str(config.port), "PORT_STRICT": "1"}
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cassandra_hoard.main:create_app", "--factory", "--reload", "--host", "127.0.0.1", "--port", str(config.port)],
        cwd=ROOT,
        env=env,
    )
    npx = shutil.which("npx") or shutil.which("npx.cmd") or "npx"
    vite = subprocess.Popen([npx, "vite"], cwd=ROOT, env=env, shell=sys.platform == "win32")
    try:
        while api.poll() is None and vite.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in (api, vite):
            if child.poll() is None:
                child.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
