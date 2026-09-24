"""Start Cassandra's Hoard on a free port and open the browser (Windows: os.startfile)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cassandra_hoard.config import Config  # noqa: E402
from cassandra_hoard.port import find_available_port  # noqa: E402


def main() -> int:
    config = Config.from_env()
    port = config.port if config.port_strict else find_available_port(config.port)
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "CASSANDRA_PORT": str(port), "PORT_STRICT": "1"}
    child = subprocess.Popen([sys.executable, "-m", "cassandra_hoard"], cwd=ROOT, env=env)
    for _ in range(150):
        if child.poll() is not None:
            return child.returncode or 1
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=0.5) as response:
                if response.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.2)
    print(f"Abriendo {url}", flush=True)
    if sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        print(f"Open {url} in your browser.")
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.terminate()
        return 0


if __name__ == "__main__":
    sys.exit(main())
