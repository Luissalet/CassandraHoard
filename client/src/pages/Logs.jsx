import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Empty } from "../components/ui.jsx";
import { clock } from "../format.js";

const LEVEL_COLORS = { error: "var(--danger)", warning: "var(--warn)", info: "var(--muted)", debug: "#5d6882" };

export default function Logs({ query }) {
  const { t, lang } = useApp();
  const [services, setServices] = useState([]);
  const [form, setForm] = useState({ q: query?.get("q") || "", service: query?.get("service") || "", level: "", since: "24h", at: query?.get("at") || "" });
  const [data, setData] = useState(null);
  const [sources, setSources] = useState(null);
  const [showSources, setShowSources] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(async (f) => {
    try {
      setData(await api.logs({ q: f.q, service: f.service, level: f.level, since: f.at ? undefined : f.since, at: f.at || undefined, window_min: f.at ? 15 : undefined, limit: 300 }));
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    api.services().then((d) => setServices(d.services)).catch(() => {});
    load(form);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });
  const submit = (e) => {
    e.preventDefault();
    load(form);
  };
  const toggleSources = async () => {
    if (!sources) setSources(await api.logSources().catch(() => ({ files: [] })));
    setShowSources(!showSources);
  };
  const names = Object.fromEntries(services.map((s) => [s.id, s.name]));

  return (
    <div className="space-y-4">
      <h1 className="text-[22px] font-semibold">{t("nav_logs")}</h1>
      <form className="panel grid gap-3 md:grid-cols-[2fr_1fr_1fr_1fr_1fr_auto] md:items-end" onSubmit={submit}>
        <label>
          <span className="label">{t("search")}</span>
          <input className="field" value={form.q} onChange={set("q")} placeholder={t("logs_placeholder")} />
        </label>
        <label>
          <span className="label">{t("filter_service")}</span>
          <select className="field" value={form.service} onChange={set("service")}>
            <option value="">{t("all_services")}</option>
            {services.map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
        </label>
        <label>
          <span className="label">{t("level")}</span>
          <select className="field" value={form.level} onChange={set("level")}>
            <option value="">{t("any_level")}</option>
            <option value="error">error</option>
            <option value="warning">warning</option>
            <option value="info">info</option>
            <option value="debug">debug</option>
          </select>
        </label>
        <label>
          <span className="label">{t("since_label")}</span>
          <input className="field" value={form.since} onChange={set("since")} placeholder={t("since_hint")} />
        </label>
        <label>
          <span className="label">{t("around")}</span>
          <input className="field" value={form.at} onChange={set("at")} placeholder={t("around_hint")} />
        </label>
        <button type="submit" className="btn btn-primary">{t("search")}</button>
      </form>
      {error && <div className="help" style={{ color: "var(--danger)" }}>{error}</div>}
      {data && data.lines.length === 0 && <Empty>{t("no_lines")}</Empty>}
      {data && data.lines.length > 0 && (
        <div className="panel mono overflow-hidden p-0">
          {data.lines.map((line) => (
            <div key={line.id} className="logline">
              <span className="num help" title={line.ts_parsed ? undefined : t("time_from_file")}>
                {clock(line.ts, lang)}{line.ts_parsed ? "" : " ·"}
              </span>
              <span className="truncate" style={{ color: LEVEL_COLORS[line.level] }}>{names[line.service] || line.service}</span>
              <span className="break-all" style={{ color: line.level === "error" ? "#ffb4ae" : "var(--ink)" }} title={line.path}>{line.line}</span>
            </div>
          ))}
        </div>
      )}
      <div>
        <button type="button" className="btn-link text-[12px]" onClick={toggleSources}>{t("sources")}</button>
        {showSources && sources && (
          <div className="panel mono mt-2 text-[11px]">
            {sources.files.length === 0 && <div className="help">—</div>}
            {sources.files.map((f) => (
              <div key={f.path} className="break-all">{f.service} · {f.path}</div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
