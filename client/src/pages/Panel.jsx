import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Lane, StatePill } from "../components/ui.jsx";
import SitesCard from "./Sites.jsx";
import { STATE_COLORS, clock, duration, gb } from "../format.js";

const GROUP_ORDER = ["faustus", "llm", "comfyui", "hub", "apps", "custom"];

function Summary({ status, t }) {
  const counts = status?.counts || {};
  const system = status?.system;
  const gpus = status?.gpu?.now || [];
  return (
    <div className="grid gap-3 md:grid-cols-3">
      <div className="panel">
        <div className="flex flex-wrap gap-2">
          {["up", "degraded", "down", "foreign", "never_seen"].map((state) =>
            counts[state] ? (
              <span key={state} className="chip" style={{ fontSize: 12 }}>
                <span className="dot" style={{ background: STATE_COLORS[state] }} />
                <span className="num">{counts[state]}</span> {t(`state_${state}`)}
              </span>
            ) : null,
          )}
        </div>
        <div className="mt-3 flex items-baseline gap-2">
          <span className="num text-[26px] font-semibold" style={{ color: status?.incidents?.open ? "var(--danger)" : "var(--ok)" }}>
            {status?.incidents?.open ?? "—"}
          </span>
          <span className="help">{t("open_incidents")} · {status?.incidents?.last_24h ?? 0} {t("last_24h").toLowerCase()}</span>
        </div>
      </div>
      <div className="panel">
        <div className="label">{t("machine")}</div>
        <div className="text-[13px]">
          {system?.boot_time ? (
            <>
              {t("booted")} {clock(system.boot_time)} · {t("uptime")} {system.uptime}
            </>
          ) : (
            "—"
          )}
        </div>
        {status?.poller?.error && <div className="help mt-2" style={{ color: "var(--warn)" }}>{status.poller.error}</div>}
        {status?.registry_error && <div className="help mt-2" style={{ color: "var(--warn)" }}>{status.registry_error}</div>}
      </div>
      <div className="panel">
        <div className="label">{t("gpus_now")}</div>
        {gpus.length === 0 && <div className="help">{t("no_gpu")}</div>}
        {gpus.map((g) => (
          <div key={g.gpu} className="mb-2 text-[12px]">
            <div className="flex justify-between">
              <span>GPU {g.gpu}</span>
              <span className="num help">
                {gb(g.mem_used_mb)} / {gb(g.mem_total_mb)} · {gb(g.mem_free_mb)} {t("free")} · {g.util_pct ?? "—"}%
              </span>
            </div>
            <div className="bar mt-1">
              <span style={{ width: `${g.mem_pct}%`, background: g.mem_pct > 90 ? "var(--danger)" : "var(--accent)" }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// What Faustus is waiting on the person for (read with a read-only token).
function FaustusWaiting({ info, t }) {
  if (!info || !info.enabled) return null;
  if (info.ok === false) {
    if (info.reason === "no_token") return null;
    return (
      <div className="panel help" data-testid="faustus-waiting">
        {t("faustus_wait_title")}: {t(`faustus_reason.${info.reason}`)}
      </div>
    );
  }
  if (info.ok !== true) return null;
  const waiting = info.waiting_on_you || 0;
  const longWaits = info.long_waits || [];
  return (
    <div className="panel" data-testid="faustus-waiting">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="label">{t("faustus_wait_title")}</span>
        <span className="help">{t("faustus_wait_rule").replace("{n}", info.wait_min)}</span>
      </div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="num text-[22px] font-semibold" style={{ color: longWaits.length ? "var(--warn)" : waiting ? "var(--accent)" : "var(--ok)" }}>
          {waiting}
        </span>
        <span className="help">{t("faustus_waiting_on_you")}{info.stalled ? ` · ${info.stalled} ${t("faustus_stalled")}` : ""}</span>
      </div>
      {longWaits.map((w) => (
        <div key={`${w.session_id}:${w.kind}`} className="mt-1 text-[12px]" data-testid="faustus-long-wait">
          <span className="chip" style={{ fontSize: 11 }}>{t(`faustus_kind.${w.kind}`)}</span>{" "}
          <span>{w.label || w.session_id}</span>{" "}
          <span className="help num">{Math.round(w.waited_min)} min</span>
        </div>
      ))}
    </div>
  );
}

// What Faustus is running right now: runs with their sub-agents indented under
// them, and the period budget. Polled only while the Panel is on screen.
const FARM_POLL_MS = 4000;
const FARM_STATE_COLORS = {
  running: "var(--ok)", queued: "var(--accent)", waiting: "var(--warn)", paused: "var(--muted)",
  stalled: "var(--danger)", verifying: "var(--accent)", cancelling: "var(--warn)",
};

function useFarm() {
  const [farm, setFarm] = useState(null);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (document.hidden) return;
      try {
        const data = await api.faustusFarm();
        if (alive) setFarm(data);
      } catch {
        if (alive) setFarm({ ok: false, reason: "cassandra" });
      }
    };
    tick();
    const timer = setInterval(tick, FARM_POLL_MS);
    const onVisible = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  return farm;
}

function budgetParts(b, t) {
  const parts = [];
  if (!b) return parts;
  const has = (v) => v !== null && v !== undefined;
  if (!b.enabled && !b.gpu) parts.push({ text: t("farm_no_target") });
  for (const r of b.providers || []) {
    let text = `${r.label}/${r.window}`;
    if (has(r.used_pct)) text += ` ${r.used_pct}% ${t("farm_used")}`;
    if (has(r.pace_pct)) text += ` (${t("farm_pace")} ${r.pace_pct}%)`;
    if (r.paused) text += ` · ${t("farm_paused")}`;
    parts.push({ text, tone: r.paused ? "warn" : undefined });
  }
  if (b.gpu) {
    let text = `${t("farm_gpu_today")} ${Math.round(b.gpu.used_s || 0)} s`;
    if (b.gpu.target_s) text += ` / ${Math.round(b.gpu.target_s)} s`;
    if (has(b.gpu.pace_pct)) text += ` (${t("farm_pace")} ${b.gpu.pace_pct}%)`;
    if (b.gpu.paused) text += ` · ${t("farm_paused")}`;
    parts.push({ text, tone: b.gpu.paused ? "warn" : undefined });
  }
  const br = b.breaker || {};
  if (br.open) {
    const again = has(br.reopens_in_s) ? ` (${t("farm_reopens", { t: duration(br.reopens_in_s) })})` : "";
    parts.push({ text: `${t("farm_breaker_open")}${again}`, tone: "danger" });
  } else {
    parts.push({ text: t("farm_breaker_closed") });
  }
  const cools = b.cooldowns || [];
  if (cools.length) {
    parts.push({ text: cools.map((c) => `${t("farm_cooldown")} ${c.endpoint} ${duration(c.remaining_s)}`).join(", "), tone: "warn" });
  } else {
    parts.push({ text: t("farm_no_cooldowns") });
  }
  if (b.interactive_active) parts.push({ text: t("farm_interactive") });
  return parts;
}

function FarmRow({ row, depth, t }) {
  const details = [row.model, t(`farm_state.${row.state}`), row.running_for, row.progress].filter(Boolean);
  const idle = row.idle_s !== undefined && row.idle_s >= 120 ? t("farm_idle", { t: duration(row.idle_s) }) : null;
  return (
    <>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px]" style={{ paddingLeft: depth * 18 }}
        data-testid="farm-row" data-kind={row.kind} data-state={row.state} data-depth={depth}>
        <span className="dot" style={{ background: FARM_STATE_COLORS[row.state] || "var(--never)" }} />
        <span className="chip" style={{ fontSize: 11 }}>{t(`farm_kind.${row.kind}`)}</span>
        {row.link ? <a href={row.link} target="_blank" rel="noreferrer" className="truncate">{row.title}</a> : <span className="truncate">{row.title}</span>}
        <span className="help num">{details.join(" · ")}</span>
        {idle && <span className="help" style={{ color: "var(--warn)" }}>{idle}</span>}
        {row.progress_pct !== undefined && (
          <span style={{ display: "inline-block", width: 64 }}>
            <div className="bar"><span style={{ width: `${Math.max(0, Math.min(100, row.progress_pct))}%`, background: "var(--accent)" }} /></div>
          </span>
        )}
      </div>
      {(row.children || []).map((child) => <FarmRow key={child.id} row={child} depth={depth + 1} t={t} />)}
    </>
  );
}

function FarmCard({ t }) {
  const farm = useFarm();
  if (!farm) return null;
  if (farm.ok === false) {
    return (
      <div className="panel help" data-testid="farm-card" data-ok="false">
        {t("farm_title")}: {t(`farm_reason.${farm.reason}`)}
      </div>
    );
  }
  const runs = farm.runs || [];
  const parts = budgetParts(farm.budget, t);
  return (
    <div className="panel" data-testid="farm-card" data-ok="true">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="label">{t("farm_title")}</span>
        <span className="help num">{farm.counts?.total || 0}</span>
      </div>
      {runs.length === 0 && <div className="help mt-1">{t("farm_none")}</div>}
      {runs.map((row) => <FarmRow key={row.id} row={row} depth={0} t={t} />)}
      {parts.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[12px]" data-testid="farm-budget">
          <span className="label">{t("farm_budget")}</span>
          {parts.map((p, i) => (
            <span key={i} className="help" style={p.tone ? { color: `var(--${p.tone})` } : undefined}>{p.text}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function Detail({ service, onRestart, t, lang }) {
  return (
    <div className="grid gap-2 px-3 pb-3 pt-1 text-[12px] md:grid-cols-[1fr_auto]" style={{ background: "var(--surface-2)" }}>
      <div className="space-y-1">
        <div className="help">
          {service.url} · {t(`kind.${service.kind}`)}
          {service.since && (
            <>
              {" "}· {t("since")} {clock(service.since, lang)} ({service.for})
            </>
          )}
        </div>
        {service.detail && <div>{t("detail")}: {service.detail}</div>}
        {service.note && <div className="help">{service.note}</div>}
        {service.pid && (
          <div>
            {t("pid")}: {service.process || "?"} (pid {service.pid}){service.uptime ? ` · ${t("uptime")} ${service.uptime}` : ""}
          </div>
        )}
        {service.latency_ms !== null && service.latency_ms !== undefined && <div>{t("latency")}: {Math.round(service.latency_ms)} ms</div>}
        {service.open_incident && (
          <div>
            <a href={`#/incidents/${service.open_incident}`}>{t("see_incident")} #{service.open_incident}</a>
          </div>
        )}
      </div>
      {service.can_restart && service.state !== "foreign" && (
        <div>
          <button type="button" className="btn btn-sm" onClick={() => onRestart(service)}>
            {service.state === "up" || service.state === "degraded" ? t("restart") : t("start")}
          </button>
        </div>
      )}
    </div>
  );
}

export default function Panel() {
  const { status, act, t, lang, refresh } = useApp();
  const [lanes, setLanes] = useState(null);
  const [services, setServices] = useState([]);
  const [open, setOpen] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [lanesData, servicesData] = await Promise.all([api.lanes(24), api.services()]);
      setLanes(lanesData);
      setServices(servicesData.services);
    } catch {
      // the shell shows the connection error
    }
  }, []);
  useEffect(() => {
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, [load]);

  const checkNow = async () => {
    setBusy(true);
    await act(() => api.poll());
    await load();
    await refresh();
    setBusy(false);
  };

  const restart = async (service) => {
    if (!window.confirm(t("restart_confirm", { name: service.name }))) return;
    await act(() => api.restart(service.id), (r) => t("restarted", { detail: r.detail }));
    setTimeout(load, 3000);
  };

  const laneById = Object.fromEntries((lanes?.lanes || []).map((l) => [l.id, l]));
  const groups = GROUP_ORDER.map((g) => [g, services.filter((s) => s.group === g)]).filter(([, list]) => list.length);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-[22px] font-semibold">{t("nav_panel")}</h1>
        <button type="button" className="btn btn-primary" onClick={checkNow} disabled={busy}>
          {busy ? t("checking") : t("check_now")}
        </button>
      </div>
      <Summary status={status} t={t} />
      <FaustusWaiting info={status?.faustus_attention} t={t} />
      <FarmCard t={t} />
      <SitesCard />
      <div className="flex items-center justify-between text-[12px]">
        <span className="help">{t("lanes_24h")}</span>
        <span className="help num">−24 {t("hours_ago")} · · · {t("now")}</span>
      </div>
      {groups.map(([group, list]) => (
        <section key={group} className="panel p-0">
          <h2 className="px-3 pb-1 pt-3 text-[13px] font-semibold" style={{ color: "var(--muted)" }}>{t(`groups.${group}`)}</h2>
          <div>
            {list.map((service) => {
              const lane = laneById[service.id];
              return (
                <React.Fragment key={service.id}>
                  <div className="svc-row" onClick={() => setOpen(open === service.id ? null : service.id)} role="button" tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && setOpen(open === service.id ? null : service.id)} aria-expanded={open === service.id}>
                    <div className="min-w-0">
                      <div className="truncate font-medium">{service.name}</div>
                      <div className="help num text-[11px]">
                        :{service.port}
                        {service.latency_ms ? ` · ${Math.round(service.latency_ms)} ms` : ""}
                        {lane?.uptime_pct !== null && lane?.uptime_pct !== undefined ? ` · ${lane.uptime_pct}%` : ""}
                      </div>
                    </div>
                    <div>
                      <StatePill state={service.state} t={t} />
                    </div>
                    <div className="lane-cell">
                      {lane && lanes ? (
                        <Lane segments={lane.segments} since={lanes.since} until={lanes.until} gaps={lanes.gaps} reboots={lanes.reboots} t={t} lang={lang} />
                      ) : (
                        <div className="lane" />
                      )}
                    </div>
                  </div>
                  {open === service.id && <Detail service={service} onRestart={restart} t={t} lang={lang} />}
                </React.Fragment>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}
