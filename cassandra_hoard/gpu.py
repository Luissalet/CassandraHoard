"""GPU sampling through ``nvidia-smi`` (skipped silently when it is not installed)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Optional

QUERY = ["--query-gpu=index,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"]


@dataclass
class GpuSample:
    gpu: int
    mem_used_mb: float
    mem_total_mb: float
    util_pct: Optional[float]

    @property
    def mem_pct(self) -> float:
        return round(100.0 * self.mem_used_mb / self.mem_total_mb, 1) if self.mem_total_mb else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"gpu": self.gpu, "mem_used_mb": self.mem_used_mb, "mem_total_mb": self.mem_total_mb,
                "mem_free_mb": round(self.mem_total_mb - self.mem_used_mb, 1), "mem_pct": self.mem_pct, "util_pct": self.util_pct}


def _number(text: str) -> Optional[float]:
    text = text.strip().replace("MiB", "").replace("%", "").strip()
    try:
        return float(text)
    except ValueError:
        return None  # "[N/A]", "[Not Supported]"


def parse_nvidia_smi(text: str) -> list[GpuSample]:
    """``0, 8123 MiB, 24564 MiB, 37 %`` (with or without units) → samples; malformed lines are skipped."""
    out: list[GpuSample] = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        index, used, total = _number(parts[0]), _number(parts[1]), _number(parts[2])
        if index is None or used is None or total is None:
            continue
        util = _number(parts[3]) if len(parts) > 3 else None
        out.append(GpuSample(int(index), used, total, util))
    return out


def find_nvidia_smi() -> Optional[str]:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if sys.platform.startswith("win"):
        for candidate in (r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.isfile(candidate):
                return candidate
    return None


class GpuReader:
    """Callable: returns the current samples, or [] when there is no NVIDIA GPU / driver."""

    def __init__(self) -> None:
        self.exe = find_nvidia_smi()
        self.error: Optional[str] = None if self.exe else "nvidia-smi not found"

    def __call__(self) -> list[GpuSample]:
        if not self.exe:
            return []
        kwargs: dict[str, Any] = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run([self.exe, *QUERY], capture_output=True, text=True, timeout=8, **kwargs)
        except Exception as error:  # noqa: BLE001
            self.error = str(error)
            return []
        if result.returncode != 0:
            self.error = (result.stderr or result.stdout).strip()[:300] or f"exit code {result.returncode}"
            return []
        self.error = None
        return parse_nvidia_smi(result.stdout)
