"""Pick a free localhost port starting from the preferred one."""

from __future__ import annotations

import socket


def can_listen(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def find_available_port(preferred: int, attempts: int = 100, host: str = "127.0.0.1") -> int:
    for offset in range(attempts):
        port = preferred + offset
        if port > 65535:
            break
        if can_listen(port, host):
            return port
    raise RuntimeError(f"No free port between {preferred} and {min(65535, preferred + attempts - 1)}.")


def free_port() -> int:
    """Any free port (used by tests)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
