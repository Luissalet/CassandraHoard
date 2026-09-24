import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Empty } from "../components/ui.jsx";
import { STATE_COLORS, clock, duration, gb } from "../format.js";

function Context({ item, t, lang }) {
  const ctx = item.context || {};
  const proc = ctx.process;
  return (
    <div className="mt-3 space-y-3 text-[12px]">
      <ul className="list-disc space-y-1 pl-5">
        {(item.explanation || []).map((sentence, i) => (
          <li key={i}>{sentence}</li>
        ))}
      </ul>
      {!item.context_final && <div className="help">{t("context_pending")}</div>}
      {ctx.correlated?.length > 0 && (
        <div>
          <div className="label">{t("what_else")}</div>
          {ctx.correlated.map((c, i) => (
            <div key={i} className="num">
              <span className="dot mr-2" style={{ background: STATE_COLORS[c.to] || "var(--never)" }} />
              {clock(c.ts, lang)} ({c.delta_s > 0 ? "+" : ""}{Math.round(c.delta_s)} s) · {c.name}: {c.from} → {c.to}
              {c.detail ? <span className="help"> · {c.detail}</span> : null}
            </div>
          ))}
        </div>
      )}
      {ctx.gpu?.length > 0 && (
        <div>
          <div className="label">{t("gpu_before")}</div>
          {ctx.gpu.map((g) => (
            <div key={g.gpu} className="num">
              GPU {g.gpu}: {g.last ? `${gb(g.last.mem_used_mb)} / ${gb(g.last.mem_total_mb)} (${g.last.mem_pct}%) · ${t("util")} ${g.last.util_pct ?? "—"}%` : "—"} · {t("peak")} {g.peak_pct}% {clock(g.peak_ts, lang)}
            </div>
          ))}
        </div>
      )}
      {proc && (
        <div>
          <div className="label">{t("process")}</div>
          <div className="mono">
            pid {proc.pid} {proc.name} · {proc.alive === true ? t("alive") : proc.alive === false ? t("gone") : "?"}
            {proc.cmdline ? <div className="help break-all">{proc.cmdline}</div> : null}
          </div>
        </div>
      )}
      {ctx.log_tail?.length > 0 && (
        <div>
          <div className="label">{t("log_tail")}</div>
          <pre className="mono overflow-x-auto rounded-md p-2" style={{ background: "var(--field)", whiteSpace: "pre-wrap" }}>
            {ctx.log_tail.map((l) => l.line).join("\n")}
          </pre>
        </div>
      )}
      {item.actions?.length > 0 && (
        <div>
          <div className="label">{t("actions")}</div>
          {item.actions.map((a, i) => (
            <div key={i} className="num">
              {clock(a.ts, lang)} · {a.kind}
              {a.trigger ? ` (${a.trigger}${a.method ? `, ${a.method}` : ""})` : ""}
              {a.ok !== undefined ? ` · ${a.ok ? "ok" : "✗"}` : ""} {a.detail ? `· ${a.detail}` : ""}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Incidents({ param }) {
  const { t, lang } = useApp();
  const [services, setServices] = useState([]);
  const [filters, setFilters] = useState({ service: "", open_only: false, since: "7d", at: "" });
  const [data, setData] = useState(null);
  const [open, setOpen] = useState(param ? Number(param) : null);
  const [error, setError] = useState(null);

  const load = useCallback(async (f) => {
    try {
      const params = { service: f.service, open_only: f.open_only || undefined, since: f.at ? undefined : f.since, at: f.at || undefined, window_min: f.at ? 30 : undefined };
      setData(await api.incidents(params));
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    api.services().then((d) => setServices(d.services)).catch(() => {});
    load(filters);
    const timer = setInterval(() => load(filters), 20000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (param) setOpen(Number(param));
  }, [param]);

  const submit = (e) => {
    e.preventDefault();
    load(filters);
  };
  const set = (key) => (e) => setFilters({ ...filters, [key]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const now = Date.now() / 1000;

  return (
    <div className="space-y-4">
      <h1 className="text-[22px] font-semibold">{t("nav_incidents")}</h1>
      <form className="panel grid gap-3 md:grid-cols-[1fr_1fr_1fr_auto_auto] md:items-end" onSubmit={submit}>
        <label>
          <span className="label">{t("filter_service")}</span>
          <select className="field" value={filters.service} onChange={set("service")}>
            <option value="">{t("all_services")}</option>
            {services.map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
        </label>
        <label>
          <span className="label">{t("since_label")}</span>
          <input className="field" value={filters.since} onChange={set("since")} placeholder={t("since_hint")} />
        </label>
        <label>
          <span className="label">{t("around")}</span>
          <input className="field" value={filters.at} onChange={set("at")} placeholder={t("around_hint")} />
        </label>
        <label className="flex items-center gap-2 text-[13px]">
          <input type="checkbox" checked={filters.open_only} onChange={set("open_only")} /> {t("open_only")}
        </label>
        <button type="submit" className="btn btn-primary">{t("search")}</button>
      </form>
      {error && <div className="help" style={{ color: "var(--danger)" }}>{error}</div>}
      {data && data.incidents.length === 0 && <Empty>{t("no_incidents")}</Empty>}
      <div className="space-y-2">
        {(data?.incidents || []).map((item) => {
          const expanded = open === item.id;
          const end = item.closed_at;
          return (
            <article key={item.id} className="panel" style={{ borderColor: item.open ? "#e5534b88" : "var(--line)" }}>
              <button type="button" className="w-full cursor-pointer text-left" style={{ background: "none", border: 0, color: "inherit", font: "inherit", padding: 0 }}
                onClick={() => setOpen(expanded ? null : item.id)} aria-expanded={expanded}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="dot" style={{ background: item.open ? "var(--danger)" : item.kind === "restart" ? "var(--warn)" : "var(--ok)" }} />
                  <span className="font-semibold">{item.name}</span>
                  <span className="chip">{item.kind === "restart" ? "restart" : `${item.from_state} → ${item.to_state}`}</span>
                  <span className="help num">{clock(item.opened_at, lang)}</span>
                  <span className="help">· {end ? `${t("lasted")} ${duration(end - item.opened_at)}` : `${t("still_open")} (${duration(now - item.opened_at)})`}</span>
                  <span className="help ml-auto">#{item.id}</span>
                </div>
                {item.probable_cause && (
                  <div className="mt-2 text-[13px]">
                    <span className="help">{t("probable_cause")}: </span>
                    {item.probable_cause}
                  </div>
                )}
              </button>
              {expanded && <Context item={item} t={t} lang={lang} />}
            </article>
          );
        })}
      </div>
    </div>
  );
}
