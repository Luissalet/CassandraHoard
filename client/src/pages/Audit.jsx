import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Empty } from "../components/ui.jsx";
import { clock } from "../format.js";

function eventText(e) {
  const data = { ...(e.data || {}) };
  for (const k of ["tool", "ok", "ms", "caller"]) delete data[k];
  const parts = [];
  if (e.tool) parts.push(e.tool);
  if (e.ms !== null && e.ms !== undefined) parts.push(`${Math.round(e.ms)} ms`);
  if (e.caller) parts.push(`← ${e.caller}`);
  const rest = JSON.stringify(data);
  if (rest && rest !== "{}") parts.push(rest);
  return parts.join(" · ");
}

export default function Audit({ query }) {
  const { t, lang } = useApp();
  const [form, setForm] = useState({ q: query?.get("q") || "", type: query?.get("type") || "", source: query?.get("source") || "", failed: false, since: "24h", at: query?.get("at") || "" });
  const [data, setData] = useState(null);
  const [stats, setStats] = useState(null);
  const [secrets, setSecrets] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async (f) => {
    try {
      setData(await api.audit({ q: f.q, type: f.type, source: f.source, failed: f.failed, since: f.at ? undefined : f.since, at: f.at || undefined, window_min: f.at ? 15 : undefined, limit: 300 }));
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);
  useEffect(() => {
    load(form);
    api.auditStats({ since: "7d" }).then(setStats).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const submit = (e) => { e.preventDefault(); load(form); };
  const sync = async () => { await api.auditSync().catch(() => {}); load(form); api.auditStats({ since: "7d" }).then(setStats).catch(() => {}); };
  const runSecrets = async () => { setSecrets(await api.secrets().catch((e) => ({ error: e.message }))); };

  return (
    <div className="space-y-4">
      <h1 className="text-[22px] font-semibold">{t("nav_audit")}</h1>
      <p className="help">{t("audit_intro")}{data && data.bus ? " · " + t("audit_bus", data.bus) : ""}</p>
      <form className="panel grid gap-3 md:grid-cols-[2fr_1fr_1fr_1fr_1fr_auto_auto] md:items-end" onSubmit={submit}>
        <label><span className="label">{t("search")}</span><input className="field" value={form.q} onChange={set("q")} placeholder={t("audit_placeholder")} /></label>
        <label><span className="label">{t("audit_type")}</span><input className="field" value={form.type} onChange={set("type")} placeholder="agent.call" /></label>
        <label><span className="label">{t("audit_source")}</span><input className="field" value={form.source} onChange={set("source")} placeholder="scribe" /></label>
        <label><span className="label">{t("since_label")}</span><input className="field" value={form.since} onChange={set("since")} placeholder={t("since_hint")} /></label>
        <label><span className="label">{t("around")}</span><input className="field" value={form.at} onChange={set("at")} placeholder={t("around_hint")} /></label>
        <label className="flex items-center gap-2 pb-2"><input type="checkbox" checked={form.failed} onChange={set("failed")} /><span className="label m-0">{t("audit_failed")}</span></label>
        <div className="flex gap-2"><button type="submit" className="btn btn-primary">{t("search")}</button><button type="button" className="btn" onClick={sync}>{t("audit_sync")}</button></div>
      </form>
      {error && <div className="help" style={{ color: "var(--danger)" }}>{error}</div>}
      {data && data.events.length === 0 && <Empty>{t("audit_no_events")}</Empty>}
      {data && data.events.length > 0 && (
        <div className="panel mono overflow-hidden p-0">
          {data.events.map((e) => (
            <div key={e.id} className="logline">
              <span className="num help">{clock(e.ts, lang)}</span>
              <span className="truncate" style={{ color: e.ok === false ? "var(--danger)" : "var(--accent, #d64a8a)" }}>{e.type}</span>
              <span className="break-all" title={JSON.stringify(e.data)}><b>{e.source}</b> {eventText(e)}</span>
            </div>
          ))}
        </div>
      )}
      {stats && stats.agent_calls && stats.agent_calls.length > 0 && (
        <div className="panel">
          <h2 className="mb-2 font-semibold">{t("audit_stats")}</h2>
          <table className="w-full text-[12px]">
            <thead><tr className="help text-left"><th>{t("audit_col_app")}</th><th>{t("audit_col_tool")}</th><th>{t("audit_col_count")}</th><th>{t("audit_col_failed")}</th><th>{t("audit_col_avg")}</th><th>{t("audit_col_max")}</th></tr></thead>
            <tbody>
              {stats.agent_calls.map((c) => (
                <tr key={c.app + c.tool}><td>{c.app}</td><td className="mono">{c.tool}</td><td>{c.count}</td><td style={{ color: c.failed ? "var(--danger)" : undefined }}>{c.failed}</td><td>{c.avg_ms ?? "—"}</td><td>{c.max_ms ?? "—"}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="panel">
        <div className="flex items-center justify-between"><h2 className="font-semibold">{t("secrets_title")}</h2><button type="button" className="btn" onClick={runSecrets}>{t("secrets_run")}</button></div>
        {secrets && secrets.error && <div className="help" style={{ color: "var(--danger)" }}>{secrets.error}</div>}
        {secrets && !secrets.error && secrets.with_problems === 0 && <div className="help mt-2">{t("secrets_ok")} ({secrets.checked})</div>}
        {secrets && !secrets.error && secrets.with_problems > 0 && (
          <ul className="mt-2 space-y-1 text-[12.5px]">
            {secrets.summary.map((line) => <li key={line} style={{ color: "var(--warn)" }}>{line}</li>)}
          </ul>
        )}
      </div>
    </div>
  );
}
