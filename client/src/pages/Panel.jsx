import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Lane, StatePill } from "../components/ui.jsx";
import { STATE_COLORS, clock, gb } from "../format.js";

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
