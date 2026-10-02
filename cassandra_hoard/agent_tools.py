"""Tools exposed to the assistant. One list drives /api/agent/* and mcp_server.py."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from . import faustus_attention, faustus_farm, views
from .audit import secrets_audit
from .incidents import explain
from .poller import OK_STATES
from .services import Services
from .sites import SITE_PREFIX
from .times import clock, duration, iso, window

AGENT_INSTRUCTIONS = """Cassandra's Hoard watches every local AI service on this PC (the Faustus workspace and its test instances, llama-server, Ollama, ComfyUI, the Hoard Hub launcher and every Hoard app) and remembers what went up and down, when, what else changed at that moment, what the logs said and what the GPUs were doing.
Use it to answer: "¿está X caído?" / "is X down?" → svc_status; "¿qué pasó a las 04:00?" / "what happened at 4 am?" → svc_incidents with at="04:00" (then svc_why_down or logs_search around that time); "¿por qué se paró Y?" / "why did Y stop?" → svc_why_down; "¿qué GPU está libre?" / "which GPU is free?" → gpu_timeline (the `now` block has free memory per GPU); the full up/down timeline of one service → svc_history.
Always quote the timestamps you got (local time) and the probable cause as Cassandra states it; say "probable", it is a heuristic. If Cassandra says no clear cause, keep the cause unknown. An empty bus result only means no events were stored: inspect bus_observation before claiming an agent did nothing. A foreground window from Funes shows activity, not an edit, restart or cause. Process start time is not proven service uptime. A service marked never_seen was never running while Cassandra watched: that is not an incident.
Times accept ISO (2026-09-24T04:00), a clock time (04:00 = the last 04:00), or an age (2h, 30m, 1d).
"¿me está esperando Faustus?" / "is Faustus waiting for me?" → faustus_attention (approvals and questions waiting on the person, stalled runs, how long each has waited).
"¿qué está haciendo Faustus ahora?" / "what is Faustus running right now?" / "¿cuánto presupuesto queda?" → faustus_farm (every chat turn in flight with the sub-agents it started grouped under it, dispatched jobs and their workers, a night shift, workflow runs and scheduled tasks, each with state, model, age and progress; plus the period budget: window used against its pace line, GPU seconds today, failure breaker, provider cooldowns). It carries names and states, never what anyone typed. Quote its titles, states and ages; a stalled item is one that stopped sending events, not a proven failure.
Public websites (the sites other people use, watched from outside; the list is data/sites.json): "¿está mi web caída?" / "is my website up?", "¿cuándo caduca el certificado o el dominio?" / "when does the certificate or the domain expire?" → sites_status (state, latency, certificate days left, domain days left from RDAP, last change; a domain whose RDAP answer is unknown is unknown, not fine); "¿ha estado caída esta semana?" / "was it down this week?" → site_history. A site is down only after two failed checks in a row; its incidents (kind site) also appear in svc_incidents as service site:<id>. Certificate and domain dates are checked, not guessed: quote them with their days left.
svc_restart, svc_watch and sites_watch change things: never restart a service or edit the watch list (services or sites) unless the user explicitly asks for it."""


class Empty(BaseModel):
    pass


class StatusArgs(BaseModel):
    service: Optional[str] = Field(None, max_length=120, description="Service id, name or port (e.g. 'faustus', 'Argus', '8081'); omit for every service.")


class WindowArgs(BaseModel):
    since: Optional[str] = Field(None, max_length=40, description="Start: ISO time, clock time ('03:50') or age ('2h'). Default 24 h ago.")
    until: Optional[str] = Field(None, max_length=40, description="End (same formats). Default now.")
    at: Optional[str] = Field(None, max_length=40, description="Centre of a window instead of since/until, e.g. '04:00'.")
    window_min: float = Field(15, ge=1, le=1440, description="Half-width of the `at` window in minutes.")


class IncidentsArgs(WindowArgs):
    service: Optional[str] = Field(None, max_length=120, description="Only this service (id, name or port).")
    open_only: bool = Field(False, description="Only incidents that are still open (service still down).")
    limit: int = Field(20, ge=1, le=200)


class WhyArgs(BaseModel):
    service: str = Field(..., min_length=1, max_length=120, description="Service id, name or port.")
    incident_id: Optional[int] = Field(None, ge=1, description="A specific incident instead of the latest one.")


class LogsArgs(WindowArgs):
    query: str = Field("", max_length=300, description="Words that must all appear in the line (case-insensitive); empty = any line.")
    service: Optional[str] = Field(None, max_length=120, description="Only lines from this service's logs.")
    level: Optional[str] = Field(None, pattern="^(error|warning|info|debug)$", description="error, warning (includes errors), info or debug.")
    limit: int = Field(50, ge=1, le=500)


class GpuArgs(WindowArgs):
    gpu: Optional[int] = Field(None, ge=0, le=64, description="Only this GPU index.")
    points: int = Field(48, ge=4, le=500, description="Buckets in the returned series.")


class HistoryArgs(WindowArgs):
    service: str = Field(..., min_length=1, max_length=120, description="Service id, name or port.")


class AuditSearchArgs(WindowArgs):
    query: str = Field("", max_length=300, description="Words that must appear in the event (type, source or data).")
    type: Optional[str] = Field(None, max_length=120, description="Event type glob, e.g. agent.call, scribe.*, hub.backup.*; | joins several.")
    source: Optional[str] = Field(None, max_length=60, description="Only events from this app id (or 'hub').")
    tool: Optional[str] = Field(None, max_length=100, description="Only agent.call events for this tool.")
    failed: bool = Field(False, description="Only failed agent calls.")
    limit: int = Field(50, ge=1, le=500)


class AuditStatsArgs(WindowArgs):
    pass


class SecretsArgs(BaseModel):
    service: Optional[str] = Field(None, max_length=120, description="Only this app (id, name or port); omit for every app folder.")


class RestartArgs(BaseModel):
    service: str = Field(..., min_length=1, max_length=120, description="Service id, name or port.")


class RestartPolicyArgs(BaseModel):
    enabled: Optional[bool] = Field(None, description="Restart automatically when it goes down (opt-in).")
    cmd: Optional[str | list[str]] = Field(None, description="Command to start it (shell string or argv list). Needs CASSANDRA_AGENT_COMMANDS=1 from the assistant.")
    cwd: Optional[str] = Field(None, max_length=2000, description="Working directory for cmd.")
    max_per_hour: Optional[int] = Field(None, ge=0, le=60, description="Maximum automatic restarts per hour (default 3).")


class WatchArgs(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, description="Lowercase id (letters, digits, . _ -). An existing id edits that service.")
    name: Optional[str] = Field(None, max_length=120)
    url: Optional[str] = Field(None, max_length=500, description="Base URL, e.g. http://127.0.0.1:9000 (required for a new service).")
    health_path: Optional[str] = Field(None, max_length=300, description="Path probed for health, e.g. /health (default /).")
    expect: Optional[dict[str, Any]] = Field(None, description="JSON key/value the answer must contain; value '*' = key present.")
    log_paths: Optional[list[str]] = Field(None, max_length=50, description="Log files or globs to tail for this service.")
    restart: Optional[RestartPolicyArgs] = None


class SitesStatusArgs(BaseModel):
    site: Optional[str] = Field(None, max_length=200, description="Site id, name or host (e.g. 'example.com'); omit for every site.")


class SiteHistoryArgs(WindowArgs):
    site: str = Field(..., min_length=1, max_length=200, description="Site id, name or host.")


class SitesWatchArgs(BaseModel):
    action: Literal["add", "edit", "remove"] = Field(..., description="add a site, edit one (only the fields you pass change) or remove it from the list (its history stays).")
    id: Optional[str] = Field(None, max_length=64, description="Lowercase id (letters, digits, . _ -). Required for edit and remove; add derives it from the url.")
    url: Optional[str] = Field(None, max_length=500, description="Home page, e.g. https://example.com/ (required for add).")
    name: Optional[str] = Field(None, max_length=120)
    expect_status: Optional[list[str]] = Field(None, max_length=20, description="Accepted HTTP statuses: '2xx', '301', '200-299'. Default ['2xx','3xx'].")
    keyword: Optional[str] = Field(None, max_length=200, description="Text the home page must contain (case-insensitive); empty string removes it.")
    interval_min: Optional[float] = Field(None, ge=1, le=1440, description="Minutes between checks (default 5, minimum 1).")
    enabled: Optional[bool] = Field(None, description="false pauses the checks.")
    domain: Optional[str] = Field(None, max_length=253, description="Registrable domain for the RDAP expiry lookup when the automatic guess is wrong (e.g. example.co.uk).")


class FaustusAttentionArgs(BaseModel):
    wait_min: float = Field(10, ge=0, le=1440, description="Waits at least this long (minutes) are listed as long waits.")


class FaustusFarmArgs(BaseModel):
    include_budget: bool = Field(True, description="Also return the period budget (pace, GPU seconds today, breaker, cooldowns). False = only what is running.")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    annotations: dict[str, bool]
    run: Callable[[Services, Any], Any]


def _brief_state(s: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "name", "group", "port", "state", "for", "since_iso", "latency_ms", "detail", "pid", "uptime", "open_incident", "note")
    return {k: s.get(k) for k in keys if s.get(k) not in (None, "")}


def _brief_incident(services: Services, item: dict[str, Any], now: float) -> dict[str, Any]:
    ctx = item.get("context") or {}
    end = item.get("closed_at")
    return {
        "id": item["id"], "service": item["service"], "name": services.name_of(item["service"]), "kind": item["kind"],
        "opened": iso(item["opened_at"]), "closed": iso(end) if end else None,
        "duration": duration((end or now) - item["opened_at"]) + ("" if end else " (still open)"),
        "change": f"{item['from_state']} → {item['to_state']}", "detail": item["detail"],
        "probable_cause": item["probable_cause"],
        "also_changed": [f"{c['name']} {c['from']}→{c['to']} at {clock(c['ts'])}" for c in ctx.get("correlated", [])[:6]],
        "restarts": [f"{clock(a['ts'])} {a.get('trigger')}: {'ok' if a.get('ok') else 'failed'}" for a in item.get("actions", []) if a.get("kind") == "restart"],
    }


def run_status(services: Services, args: StatusArgs) -> dict:
    now = services.clock()
    if args.service:
        service = services.resolve(args.service)
        state = services.poller.service_state(service, now)
        latest = services.incidents.latest(service.id)
        method = services.restarter.method_for(service)
        return {
            **_brief_state(state), "url": service.url, "kind": service.kind,
            "latest_incident": _brief_incident(services, latest, now) if latest else None,
            "restart": {**service.restart.to_dict(), "method": method, "auto_restarts_last_hour": services.restarter.recent(service.id)},
        }
    states = services.service_states()
    summary: dict[str, list[str]] = {}
    for s in states:
        summary.setdefault(s["state"], []).append(s["name"])
    return {
        "checked": iso(services.poller.last_tick) if services.poller.last_tick else None,
        "summary": {k: len(v) for k, v in summary.items()},
        "down": summary.get("down", []), "degraded": summary.get("degraded", []), "foreign": summary.get("foreign", []),
        "services": [_brief_state(s) for s in states],
        "system": services.poller.system_state(now),
        "gpus_now": services.poller.gpu_now,
        "note": None if services.poller.last_tick else "Cassandra has not completed a check yet; states are unknown for a few seconds.",
    }


def run_incidents(services: Services, args: IncidentsArgs) -> dict:
    now = services.clock()
    explicit = args.since or args.until or args.at
    since, until = window(args.since, args.until, args.at, args.window_min, default_hours=24 * 7 if args.open_only else 24, now=now)
    service = services.resolve(args.service) if args.service else None
    items = services.incidents.list(None if args.open_only and not explicit else since, until, service.id if service else None, args.open_only, args.limit)
    gaps = views.gaps(services.db, since, until)
    reboots = [iso(r["ts"]) for r in services.db.query("SELECT ts FROM events WHERE service = 'system' AND kind = 'reboot' AND ts BETWEEN ? AND ?", (since, until))]
    note = None
    if not items:
        note = "No incident in that window." + (" Cassandra was not running during part of it (see cassandra_gaps)." if gaps else "")
    return {
        "since": iso(since), "until": iso(until), "count": len(items),
        "incidents": [_brief_incident(services, i, now) for i in items],
        "reboots": reboots,
        "cassandra_gaps": [[iso(a), iso(b)] for a, b in gaps],
        "note": note,
    }


def run_why(services: Services, args: WhyArgs) -> dict:
    now = services.clock()
    service = services.resolve(args.service)
    state = services.poller.service_state(service, now)
    item = services.incidents.get(args.incident_id) if args.incident_id else services.incidents.latest(service.id)
    if item is None or item["service"] != service.id:
        text = f"{service.name} has no recorded incident. It is {state['state']} now"
        text += f" (since {clock(state['since'])})." if state.get("since") else "."
        return {"service": service.id, "name": service.name, "state": state["state"], "incident": None, "explanation": [text]}
    ctx = item["context"]
    bus_around = [_brief_event(e) for e in services.bus.around(item["opened_at"], 3.0, 20)]
    bus_status = services.bus.status()
    bus_observation = {
        "running": bus_status["running"],
        "last_sync": iso(bus_status["last_sync"]) if bus_status["last_sync"] else None,
        "error": bus_status["last_error"],
        "note": ("No stored events around the incident; this does not prove that no agent acted."
                 if not bus_around else None),
    }
    cause_unknown = "no clear cause" in str(item.get("probable_cause") or "").casefold()
    return {
        "service": service.id, "name": service.name, "state_now": state["state"],
        "incident": _brief_incident(services, item, now),
        "explanation": explain(item, service.name, service.port, now),
        "bus_events_around": bus_around,
        "bus_observation": bus_observation,
        "causes": [c["text"] for c in ctx.get("causes", [])],
        "evidence_assessment": {
            "cause_status": "unknown" if cause_unknown else "heuristic",
            "note": ("No recorded cause. An empty log does not establish a clean exit; "
                     "foreground desktop activity does not establish an edit or manual stop. "
                     "Process start time does not establish service uptime.") if cause_unknown else None,
        },
        "process": ctx.get("process"),
        "log_tail": [f"{clock(line['ts'])} {line['line'][:300]}" for line in (ctx.get("log_tail") or [])[-12:]],
        "context_final": item["context_final"],
    }


def run_logs(services: Services, args: LogsArgs) -> dict:
    now = services.clock()
    since, until = window(args.since, args.until, args.at, args.window_min, default_hours=24, now=now)
    service = services.resolve(args.service) if args.service else None
    rows = services.logs.search(args.query, service.id if service else None, since, until, args.limit, args.level)
    return {
        "since": iso(since), "until": iso(until), "count": len(rows), "truncated": len(rows) >= args.limit,
        "lines": [{"at": iso(r["ts"]), "service": r["service"], "level": r["level"], "line": r["line"][:600],
                   "file": r["path"].replace("\\", "/").rsplit("/", 1)[-1], "time_from_line": bool(r["ts_parsed"])} for r in rows],
        "note": None if rows else "No matching line. Check the service's log_paths (svc_status) or widen the window.",
    }


def run_gpu(services: Services, args: GpuArgs) -> dict:
    now = services.clock()
    since, until = window(args.since, args.until, args.at, args.window_min, default_hours=6, now=now)
    data = views.gpu_timeline(services.db, since, until, args.gpu, args.points)
    free = sorted((g["now"] for g in data["gpus"] if g["now"]), key=lambda n: -n["mem_free_mb"])
    data["freest_gpu"] = None
    if free:
        best = next(g for g in data["gpus"] if g["now"] is free[0])
        data["freest_gpu"] = {"gpu": best["gpu"], "mem_free_mb": free[0]["mem_free_mb"], "at": free[0]["at"]}
    if not data["gpus"]:
        data["note"] = "No GPU samples in that window (no NVIDIA GPU, nvidia-smi missing, or CASSANDRA_GPU=0)."
    return data


def run_history(services: Services, args: HistoryArgs) -> dict:
    now = services.clock()
    since, until = window(args.since, args.until, args.at, args.window_min, default_hours=24, now=now)
    service = services.resolve(args.service)
    data = views.history(services.db, service.id, since, until)
    ever = {service.id} if services.poller.current.get(service.id) and services.poller.current[service.id].ever_up else set()
    lane = views.lanes(services.db, [service.id], since, until, ever)[service.id]
    return {"service": service.id, "name": service.name, "since": iso(since), "until": iso(until), **data,
            "uptime_pct": views.uptime_pct(lane, since, until),
            "segments": [{"from": iso(a), "to": iso(b), "state": st, "duration": duration(b - a)} for a, b, st in lane][-50:]}


def _brief_event(e: dict[str, Any]) -> dict[str, Any]:
    out = {"at": iso(e["ts"]), "type": e["type"], "source": e["source"]}
    if e.get("tool"):
        out["tool"] = e["tool"]
    if e.get("ok") is not None:
        out["ok"] = e["ok"]
    if e.get("ms") is not None:
        out["ms"] = e["ms"]
    if e.get("caller"):
        out["caller"] = e["caller"]
    data = {k: v for k, v in (e.get("data") or {}).items() if k not in ("tool", "ok", "ms", "caller")}
    if data:
        out["data"] = data
    return out


def run_audit_search(services: Services, args: AuditSearchArgs) -> dict:
    now = services.clock()
    since, until = window(args.since, args.until, args.at, args.window_min, default_hours=24, now=now)
    kwargs: dict[str, Any] = {"query": args.query, "type": args.type, "source": args.source, "tool": args.tool,
                              "since": since, "until": until, "limit": args.limit}
    if args.failed:
        kwargs["ok"] = False
        kwargs["type"] = kwargs["type"] or "agent.call"
    events = services.bus.search(**kwargs)
    st = services.bus.status()
    note = None
    if not events:
        note = "No event in that window." + (f" The hub bus is not reachable ({st['last_error']}); Cassandra keeps what it mirrored." if st.get("last_error") else "")
    return {"since": iso(since), "until": iso(until), "count": len(events), "events": [_brief_event(e) for e in events],
            "bus": {"hub": st["hub_url"], "stored": st["stored"], "last_sync": iso(st["last_sync"]) if st["last_sync"] else None, "error": st["last_error"]},
            "note": note}


def run_audit_stats(services: Services, args: AuditStatsArgs) -> dict:
    now = services.clock()
    since, until = window(args.since, args.until, args.at, args.window_min, default_hours=24 * 7, now=now)
    data = services.bus.stats(since, until)
    data["recent_failures"] = [_brief_event(e) for e in data["recent_failures"]]
    data.update({"since": iso(since), "until": iso(until), "bus": services.bus.status()})
    return data


def run_secrets(services: Services, args: SecretsArgs) -> dict:
    now = services.clock()
    targets = [services.resolve(args.service)] if args.service else services.registry.list()
    report = secrets_audit(targets, now=now)
    if not report["checked"]:
        report["note"] = "No app folder to audit (only apps discovered from a faustus-plugin.json have a folder)."
    return report


def run_restart(services: Services, args: RestartArgs) -> dict:
    service = services.resolve(args.service)
    cur = services.poller.current.get(service.id)
    if cur and cur.state == "foreign":
        raise ValueError(f"Another program answers on port {service.port}; Cassandra will not restart {service.name} over it.")
    running = bool(cur and cur.state in OK_STATES)
    result = services.restarter.restart(service, "manual", None, running, cur.pid if cur else None, cur.pid_started if cur else None)
    services.poller.wake()
    if not result.get("ok") and "error" in result and "method" not in result:
        raise ValueError(result["error"])
    return result


def run_watch(services: Services, args: WatchArgs) -> dict:
    raw = args.model_dump(exclude_none=True)
    restart = raw.get("restart")
    if restart is not None:
        if ("cmd" in restart or "cwd" in restart) and not services.config.agent_commands:
            raise ValueError("Restart commands can only be set in Cassandra's UI or data/services.json (or with CASSANDRA_AGENT_COMMANDS=1).")
        existing = services.registry.get(args.id)
        base = existing.restart.to_dict() if existing else {}
        raw["restart"] = {**base, **restart}
    service = services.registry.upsert_user(raw)
    services.poller.wake()
    return {"ok": True, "service": {k: v for k, v in service.to_dict().items() if k not in ("launch", "purpose")},
            "note": "Saved in data/services.json; the next check includes it."}


def run_faustus_attention(services: Services, args: FaustusAttentionArgs) -> dict[str, Any]:
    return faustus_attention.report(services.config.data_dir, wait_min=args.wait_min, clock=services.clock)


def run_faustus_farm(services: Services, args: FaustusFarmArgs) -> dict[str, Any]:
    return faustus_farm.report(services.config.data_dir, include_budget=args.include_budget, clock=services.clock)


def _resolve_site(services: Services, text: str) -> dict[str, Any]:
    site = services.sites.store.find(text)
    if site is None:
        known = ", ".join(s["id"] for s in services.sites.store.list()[:40]) or "none (add one with sites_watch)"
        raise LookupError(f"Unknown site '{text}'. Known site ids: {known}")
    return site


def run_sites_status(services: Services, args: SitesStatusArgs) -> dict:
    now = services.clock()
    items = [services.sites.status_one(_resolve_site(services, args.site), now)] if args.site else services.sites.status_all(now)
    keys = ("id", "name", "url", "state", "for", "since_iso", "status", "latency_ms", "redirect", "keyword_ok", "cause", "failing_checks",
            "last_check", "next_check", "tls", "domain", "dns", "last_change", "open_incident", "warnings", "enabled", "interval_min", "expect_status", "keyword")
    brief = [{k: item[k] for k in keys if item.get(k) not in (None, "", [])} for item in items]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {
        "checked": iso(services.sites.last_tick) if services.sites.last_tick else None,
        "summary": counts, "down": [i["name"] for i in items if i["state"] == "down"],
        "expiring": [f"{i['name']}: {w}" for i in items for w in i["warnings"] if "expires" in w],
        "sites": brief,
        "note": ("No public site is watched yet: add one with sites_watch (or in data/sites.json)." if not items else
                 "Domain days come from RDAP (cached up to a day); a domain with status unknown has no readable expiry date." if any(
                     (i.get("domain") or {}).get("status") in ("unknown", "error") for i in items) else None),
    }


def run_site_history(services: Services, args: SiteHistoryArgs) -> dict:
    now = services.clock()
    since, until = window(args.since, args.until, args.at, args.window_min, default_hours=24, now=now)
    site = _resolve_site(services, args.site)
    return services.sites.history(site, since, until)


def run_sites_watch(services: Services, args: SitesWatchArgs) -> dict:
    raw = args.model_dump(exclude_none=True)
    action = raw.pop("action")
    store = services.sites.store
    if action == "remove":
        if not args.id:
            raise ValueError("remove needs the site id.")
        site = store.remove(args.id)
        if site is None:
            raise LookupError(f"Unknown site '{args.id}'.")
        services.sites.forget(site)
        return {"ok": True, "removed": site["id"], "note": "Removed from data/sites.json; its stored history stays."}
    if action == "edit":
        if not args.id or store.find(args.id) is None:
            raise LookupError(f"Unknown site '{args.id}'. Use action 'add' for a new site.")
    elif not args.url:
        raise ValueError("add needs the site url.")
    elif store.find(args.id or args.url) is not None:
        raise ValueError("That site is already watched; use action 'edit'.")
    site, created = store.upsert(raw)
    services.sites.wake()
    return {"ok": True, "created": created, "site": services.sites.status_one(site),
            "note": "Saved in data/sites.json; the first check runs within seconds." if created else "Saved in data/sites.json."}


def _ann(read_only: bool, destructive: bool = False, idempotent: bool | None = None) -> dict[str, bool]:
    return {"readOnlyHint": read_only, "destructiveHint": destructive, "idempotentHint": read_only if idempotent is None else idempotent, "openWorldHint": False}


TOOLS: list[Tool] = [
    Tool("svc_status", "Is it up? Live status of every local AI service and app / ¿Está caído? Estado actual de servicios\nOne service or all: state (up, degraded, foreign, down, never_seen), since when, latency, pid, uptime, open incident, restart policy. Includes machine boot time and GPUs now.\nSinónimos: estado, caído, funciona, arriba, abajo, servicio, puerto, Faustus, Ollama, llama-server, ComfyUI, hub, app.", StatusArgs, _ann(True), run_status),
    Tool("svc_incidents", "Incidents (service down, restarted) in a time window / Incidencias y caídas en un intervalo de tiempo\nFilter by since/until or at='04:00' ± window_min, by service, or only open ones. Each has the probable cause and what else changed. Also lists reboots and periods when Cassandra itself was not running.\nSinónimos: qué pasó, caídas, incidencias, anoche, a las 4, se paró todo, cortes, historial.", IncidentsArgs, _ann(True), run_incidents),
    Tool("svc_why_down", "Why did a service stop? Latest incident explained with its cause / ¿Por qué se paró? Causa probable\nPlain sentences: when it fell, for how long, probable cause (reboot, sleep, several services at once, VRAM contention, Traceback in the log, process gone or hung), GPUs just before, last log lines, restart attempts.\nSinónimos: por qué, causa, motivo, se cayó, se paró, crash, error, explicación.", WhyArgs, _ann(True), run_why),
    Tool("logs_search", "Search the logs of every local service by words, service and time / Buscar en los logs por texto y hora\nLines tailed from each app's data/logs, the launcher's logs and configured files, with time and level (error, warning, info). Use at='04:00' to read what happened around a moment.\nSinónimos: logs, registros, trazas, error, traceback, buscar en logs, qué dijo, mensajes.", LogsArgs, _ann(True), run_logs),
    Tool("gpu_timeline", "GPU memory and load over time, peaks and which GPU is free now / Memoria de GPU en el tiempo, cuál está libre\nCompact series per GPU (mem % max/avg, util % max) from nvidia-smi samples, peak moments, current free memory and the freest GPU.\nSinónimos: GPU, VRAM, memoria de vídeo, tarjeta gráfica, libre, ocupada, carga, nvidia, picos.", GpuArgs, _ann(True), run_gpu),
    Tool("svc_history", "Up/down timeline of one service: state changes and uptime / Historial de estados de un servicio\nState at the start of the window, every change (state, pid restart, restart action) with its time, segments and uptime percentage.\nSinónimos: historial, cronología, cuándo estuvo caído, disponibilidad, uptime, cambios de estado.", HistoryArgs, _ann(True), run_history),
    Tool("audit_search", "What the assistant and the apps did: agent calls and app events by time / Qué hizo el asistente, auditoría\nThe family bus mirrored from the Hoard Hub: one agent.call per tool run (app, tool, ok, ms, who asked), app milestones (scribe.transcript.done, links.watch.new), hub actions (backups, rules, starts). Filter by words, type glob, app, tool, failures, and time (at='03:12').\nSinónimos: auditoría, qué hizo, quién llamó, historial de acciones, eventos, llamadas del agente, qué pasó justo antes.", AuditSearchArgs, _ann(True), run_audit_search),
    Tool("audit_stats", "Counts of agent calls per app and tool, failures, slowest, busiest callers / Estadísticas de uso del agente\nOver a window (default 7 days): events by type and source, agent.call per app/tool with failed count and avg/max ms, callers, and the last failures.\nSinónimos: estadísticas, cuántas veces, herramienta más usada, fallos, uso del agente, resumen de actividad.", AuditStatsArgs, _ann(True), run_audit_stats),
    Tool("secrets_audit", "Leaked secrets in app folders: tokens, data/ ignored, tracked .env or db / Auditoría de secretos\nPer app: token file present (and its permissions), data/ in .gitignore, secret-looking files tracked by git (mcp-token, .env, *.key, databases), .env files. Read-only.\nSinónimos: secretos, tokens, fugas, .env, gitignore, seguridad, qué está en git, credenciales.", SecretsArgs, _ann(True), run_secrets),
    Tool("faustus_attention", "Is Faustus waiting for me? Approvals, questions, stalled runs / ¿Me espera Faustus? Aprobaciones pendientes\nRead from Faustus's own attention list with a read-only token: what each chat is waiting for, for how long, the long waits (≥ wait_min) and runs that stopped sending events. Says why when it cannot read it (no token, refused, Faustus down).\nSinónimos: aprobación pendiente, me espera, atascado, turno parado, pregunta abierta, Faustus esperando, atención.", FaustusAttentionArgs, _ann(True), run_faustus_attention),
    Tool("faustus_farm", "What is Faustus running now? Runs, sub-agents, jobs, budget / ¿Qué está haciendo Faustus ahora?\nEvery chat turn in flight with the sub-agents it started, dispatched jobs and their workers, a night shift, workflow runs and scheduled tasks: title, kind, model, state, how long, progress. Plus the period budget (window used against pace, GPU seconds today, failure breaker, provider cooldowns). Names only, never message text. Says why when it cannot read it (no token, refused, Faustus down or too old).\nSinónimos: qué está corriendo, agentes en marcha, subagentes, trabajos, turno en curso, presupuesto, ritmo, GPU hoy, breaker, cooldown, Faustus ocupado.", FaustusFarmArgs, _ann(True), run_faustus_farm),
    Tool("sites_status", "Are my public websites up? Latency, certificate and domain days left / ¿Están mis webs públicas bien?\nEvery watched site (or one): up/down, HTTP status, latency, redirect, certificate days left and issuer, DNS answers, domain registration expiry (RDAP), last change, open incident, warnings.\nSinónimos: web caída, mi página, certificado, caducidad, dominio, TLS, SSL, https, renovación, disponibilidad.", SitesStatusArgs, _ann(True), run_sites_status),
    Tool("site_history", "Up/down history of one public website: changes, uptime, latency / Historial de una web pública\nState changes (down, up, certificate renewed or expiring, DNS answers changed), uptime percentage, latency avg/p95/max with a series, and its incidents with cause.\nSinónimos: historial de la web, cuándo estuvo caída, disponibilidad, uptime, latencia, certificado renovado.", SiteHistoryArgs, _ann(True), run_site_history),
    Tool("svc_restart", "Restart or start one service now (only when the user asks) / Reiniciar o arrancar un servicio ahora\nUses its restart command, the Hoard Hub launcher or the app's launch hint; recorded in the open incident. Refuses when another program holds the port.\nSinónimos: reiniciar, arrancar, levantar, relanzar, volver a encender, restart, start.", RestartArgs, _ann(False, False, False), run_restart),
    Tool("svc_watch", "Add or edit a watched service: URL, health path, logs, restart policy / Añadir o editar un servicio vigilado\nSaved in data/services.json. An existing id (also a discovered app or built-in) is edited; restart.enabled turns on automatic restarts (opt-in, max_per_hour). Only when the user asks.\nSinónimos: vigilar, monitorizar, añadir servicio, nuevo servicio, política de reinicio, logs de un servicio.", WatchArgs, _ann(False, False, True), run_watch),
    Tool("sites_watch", "Add, edit or remove a watched public website / Añadir, editar o quitar una web pública vigilada\nSaved in data/sites.json: url, expected status, keyword, check interval, pause. Removing keeps its history. Changes the watch list: only when the user asks.\nSinónimos: vigilar una web, añadir sitio, quitar sitio, monitorizar página, palabra clave, intervalo de comprobación.", SitesWatchArgs, _ann(False, False, False), run_sites_watch),
]

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_catalog() -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "annotations": t.annotations, "inputSchema": t.input_model.model_json_schema(by_alias=True)}
        for t in TOOLS
    ]


def call_tool(services: Services, name: str, arguments: dict | None) -> Any:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise KeyError(f"Unknown tool: {name}")
    args = tool.input_model.model_validate(arguments or {})
    return tool.run(services, args)
