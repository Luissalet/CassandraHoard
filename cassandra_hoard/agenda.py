"""The family agenda: what is broken right now, answered to the hub as ``GET /api/family/agenda``.

Only real dated things Cassandra knows: every incident that is still open (a service or site that is down, a job that failed
and has not been followed by a successful one) is one item of kind ``incident`` and priority ``high``. It starts when the
incident opened and runs until now, so the hub's Today list shows it on every day it stays open. Closed incidents are history
and are not listed. ``provider(...)`` is what ``fam_agenda.install_fastapi`` calls; it never raises.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable

MAX_ITEMS = 50


def _moment(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _title(name: str, item: dict[str, Any]) -> str:
    kind = item.get("kind")
    if kind == "job":
        job = (item.get("context") or {}).get("job") or {}
        label = job.get("title") or item.get("detail") or "job"
        return f"{name}: job failed — {label}"[:200]
    if kind == "site":
        return f"{name}: public site is down"[:200]
    state = item.get("to_state") or "down"
    return f"{name} is {state}"[:200]


def build_items(svc: Any, date_from: date, date_to: date, *, base_url: str) -> list[dict[str, Any]]:
    now = float(svc.clock())
    start_of = datetime.combine(date_from, dtime.min).astimezone().timestamp()
    end_of = datetime.combine(date_to + timedelta(days=1), dtime.min).astimezone().timestamp()
    items: list[dict[str, Any]] = []
    for inc in svc.incidents.list(None, None, None, True, 200):
        if inc["kind"] == "restart" or not inc.get("open"):
            continue
        opened = float(inc["opened_at"])
        if opened > end_of or now < start_of:        # an open incident lasts until now: it must overlap the window
            continue
        name = svc.name_of(inc["service"])
        item: dict[str, Any] = {
            "id": f"cassandra:incident:{inc['id']}", "title": _title(name, inc), "start": _moment(opened), "all_day": False,
            "kind": "incident", "priority": "high", "url": f"{base_url.rstrip('/')}/#/incidents/{inc['id']}",
            "detail": (inc.get("probable_cause") or inc.get("detail") or "")[:240],
        }
        if now > opened:
            item["end"] = _moment(now)
        items.append(item)
        if len(items) >= MAX_ITEMS:
            break
    return items


def make_provider(get_services: Callable[[], Any], get_base_url: Callable[[], str]) -> Callable[[date, date, str], list[dict[str, Any]]]:
    def provider(date_from: date, date_to: date, sphere: str) -> list[dict[str, Any]]:
        svc = get_services()
        if svc is None:
            return []
        try:
            return build_items(svc, date_from, date_to, base_url=get_base_url())
        except Exception:  # noqa: BLE001 - the agenda must never take the app down
            return []
    return provider
