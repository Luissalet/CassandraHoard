"""Start Cassandra's Hoard on a free port and open the browser (Windows: os.startfile)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cassandra_hoard.config import Config  # noqa: E402
from cassandra_hoard.hoard_link import net  # noqa: E402

SERVICE = "cassandra-hoard"


def main() -> int:
    config = Config.from_env()
    if net.already_running(SERVICE, config.port):  # a second copy would only start on the next port
        url = f"http://127.0.0.1:{config.port}"
        print(f"Abriendo {url}", flush=True)
        net.open_in_browser(url)
        return 0
    port = config.port if config.port_strict else net.find_available_port(config.port)
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "CASSANDRA_PORT": str(port), "PORT_STRICT": "1"}
    child = subprocess.Popen([sys.executable, "-m", "cassandra_hoard"], cwd=ROOT, env=env)
    if net.wait_healthy(url, SERVICE, timeout=30.0):
        print(f"Abriendo {url}", flush=True)
        if not net.open_in_browser(url):
            print(f"Open {url} in your browser.")
    elif child.poll() is not None:
        return child.returncode or 1
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.terminate()
        return 0


if __name__ == "__main__":
    sys.exit(main())
