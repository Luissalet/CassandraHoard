"""Time parsing and formatting shared by the API and the tools.

Accepted everywhere a time is expected (``since``, ``until``, ``at``):

* epoch seconds (``1790000000``),
* ISO 8601 (``2026-09-24T04:00``, ``2026-09-24 04:00:00``, ``2026-09-24``) — local time unless an offset is given,
* a clock time today (``04:00``, ``4:00``, ``04:00:30``); when that is in the future it means yesterday,
* a relative age: ``30m``, ``2h``, ``1d``, ``90s``, ``1w`` (also ``-2h`` or ``2h ago``),
* ``now``, ``today``, ``yesterday``.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Optional

_REL = re.compile(r"^-?\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|w|week|weeks)(\s+ago)?$", re.I)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_UNITS = {"s": 1, "sec": 1, "secs": 1, "m": 60, "min": 60, "mins": 60, "h": 3600, "hr": 3600, "hrs": 3600,
          "d": 86400, "day": 86400, "days": 86400, "w": 604800, "week": 604800, "weeks": 604800}


def parse_time(value, now: Optional[float] = None) -> Optional[float]:
    """Parse one of the accepted forms into epoch seconds; None for empty; ValueError otherwise."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    now = time.time() if now is None else now
    low = text.lower()
    if low == "now":
        return now
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if low in ("today", "hoy"):
        return midnight.timestamp()
    if low in ("yesterday", "ayer"):
        return (midnight - timedelta(days=1)).timestamp()
    if re.fullmatch(r"\d{9,11}(\.\d+)?", text):
        return float(text)
    rel = _REL.match(low)
    if rel:
        return now - float(rel.group(1)) * _UNITS[rel.group(2).lower()]
    clock = _CLOCK.match(text)
    if clock:
        hour, minute, second = int(clock.group(1)), int(clock.group(2)), int(clock.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            raise ValueError(f"Not a clock time: {text}")
        moment = midnight.replace(hour=hour, minute=minute, second=second)
        if moment.timestamp() > now + 60:
            moment -= timedelta(days=1)
        return moment.timestamp()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Unrecognised time '{text}': use ISO (2026-09-24T04:00), a clock time (04:00), or an age (2h, 30m, 1d).") from error
    return parsed.timestamp()


def window(since=None, until=None, at=None, window_min: float = 15, default_hours: float = 24, now: Optional[float] = None) -> tuple[float, float]:
    """Resolve (since, until). ``at`` ± ``window_min`` wins when given."""
    now = time.time() if now is None else now
    if at not in (None, ""):
        center = parse_time(at, now)
        span = max(1.0, float(window_min)) * 60
        return center - span, center + span
    start = parse_time(since, now)
    end = parse_time(until, now)
    end = now if end is None else end
    start = end - default_hours * 3600 if start is None else start
    if start > end:
        start, end = end, start
    return start, end


def iso(ts: Optional[float]) -> Optional[str]:
    """Local ISO time with offset, second precision."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def clock(ts: Optional[float]) -> str:
    """"04:00:12" for today, "2026-09-23 04:00:12" otherwise."""
    if ts is None:
        return "?"
    moment = datetime.fromtimestamp(ts)
    if moment.date() == datetime.now().date():
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"
