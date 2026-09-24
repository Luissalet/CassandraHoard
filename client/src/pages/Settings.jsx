import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { StatePill } from "../components/ui.jsx";

function PolicyRow({ service, userIds, onSaved, t }) {
  const { act } = useApp();
  const [policy, setPolicy] = useState(service.restart);
  const [cmd, setCmd] = useState(Array.isArray(service.restart.cmd) ? JSON.stringify(service.restart.cmd) : service.restart.cmd || "");
  const [method, setMethod] = useState(null);

  useEffect(() => {
    api.service(service.id).then((d) => setMethod(d.restart_method)).catch(() => {});
  }, [service.id]);

  const save = async (patch) => {
    const result = await act(() => api.policy(service.id, patch), t("saved"));
    if (result) {
      setPolicy(result.restart);
      setMethod(result.method);
      onSaved();
    }
  };
  const parsedCmd = () => {
    const text = cmd.trim();
    if (text.startsWith("[")) {
      try {
        return JSON.parse(text);
      } catch {
        return text;
      }
    }
    return text;
  };
  const remove = async () => {
    if (!window.confirm(t("remove_confirm", { name: service.name }))) return;
    await act(() => api.unwatch(service.id));
    onSaved();
  };

  return (
    <div className="grid gap-2 border-t px-3 py-3 md:grid-cols-[minmax(160px,1.3fr)_auto_90px_minmax(180px,2fr)_auto] md:items-center" style={{ borderColor: "var(--line)" }}>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate font-medium">{service.name}</span>
          <StatePill state={service.state} t={t} />
        </div>
        <div className="help truncate text-[11px]">
          {t(`kind.${service.kind}`)} · {service.url}{service.health_path} · {t("method")}: {t(`methods.${method || "none"}`)}
        </div>
      </div>
      <label className="flex items-center gap-2 text-[12px]">
        <button type="button" className="switch" role="switch" aria-checked={policy.enabled} aria-label={t("auto_restart")}
          onClick={() => save({ enabled: !policy.enabled })} />
        {t("auto_restart")}
      </label>
      <label className="text-[12px]">
        <span className="sr-only">{t("max_per_hour")}</span>
        <input className="field num" type="number" min="0" max="60" value={policy.max_per_hour} title={t("max_per_hour")}
          onChange={(e) => setPolicy({ ...policy, max_per_hour: Number(e.target.value) })}
          onBlur={() => save({ max_per_hour: policy.max_per_hour })} />
      </label>
      <input className="field mono" value={cmd} placeholder={t("command_hint")} aria-label={t("command")} onChange={(e) => setCmd(e.target.value)}
        onBlur={() => {
          const next = parsedCmd();
          if (JSON.stringify(next || null) !== JSON.stringify(policy.cmd || null)) save({ cmd: next || "" });
        }} />
      <div className="flex gap-2">
        {userIds.has(service.id) && (
          <button type="button" className="btn btn-sm btn-danger" onClick={remove}>{t("remove")}</button>
        )}
      </div>
    </div>
  );
}

function AddService({ onSaved, t }) {
  const { act } = useApp();
  const empty = { id: "", name: "", url: "http://127.0.0.1:", health_path: "/health", log_paths: "" };
  const [form, setForm] = useState(empty);
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });
  const submit = async (e) => {
    e.preventDefault();
    const body = { ...form, log_paths: form.log_paths.split("\n").map((s) => s.trim()).filter(Boolean) };
    if (!body.name) delete body.name;
    const result = await act(() => api.watch(body), t("saved"));
    if (result) {
      setForm(empty);
      onSaved();
    }
  };
  return (
    <form className="panel grid gap-3 md:grid-cols-4" onSubmit={submit}>
      <h2 className="text-[15px] font-semibold md:col-span-4">{t("add_service")}</h2>
      <label><span className="label">{t("id")}</span><input className="field" required value={form.id} onChange={set("id")} placeholder="whisper" /></label>
      <label><span className="label">{t("name")}</span><input className="field" value={form.name} onChange={set("name")} placeholder="Whisper server" /></label>
      <label><span className="label">{t("url")}</span><input className="field" required value={form.url} onChange={set("url")} /></label>
      <label><span className="label">{t("health_path")}</span><input className="field" value={form.health_path} onChange={set("health_path")} /></label>
      <label className="md:col-span-3">
        <span className="label">{t("log_paths")}</span>
        <textarea className="field mono" rows="2" value={form.log_paths} onChange={set("log_paths")} placeholder="C:\\tools\\whisper\\logs\\*.log" />
      </label>
      <div className="md:self-end"><button type="submit" className="btn btn-primary">{t("save")}</button></div>
    </form>
  );
}

export default function Settings() {
  const { t, status } = useApp();
  const [services, setServices] = useState([]);
  const [settings, setSettings] = useState(null);

  const load = useCallback(async () => {
    try {
      const [s, cfg] = await Promise.all([api.services(), api.settings()]);
      setServices(s.services);
      setSettings(cfg);
    } catch {
      // the shell shows the connection error
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const userIds = new Set((settings?.user_services || []).map((e) => e.id));

  return (
    <div className="space-y-4">
      <h1 className="text-[22px] font-semibold">{t("services_title")}</h1>
      <p className="help max-w-3xl">{t("services_help")}</p>
      {status && !status.auto_restart && <div className="panel" style={{ borderColor: "#e0a43a88" }}>{t("auto_restart_off")}</div>}
      <section className="panel p-0">
        <div className="help hidden px-3 pt-3 text-[11px] md:grid md:grid-cols-[minmax(160px,1.3fr)_auto_90px_minmax(180px,2fr)_auto] md:gap-2">
          <span />
          <span />
          <span>{t("max_per_hour")}</span>
          <span>{t("command")}</span>
          <span />
        </div>
        {services.map((service) => (
          <PolicyRow key={`${service.id}-${JSON.stringify(service.restart)}`} service={service} userIds={userIds} onSaved={load} t={t} />
        ))}
      </section>
      <AddService onSaved={load} t={t} />
      {settings && (
        <section className="panel">
          <h2 className="mb-2 text-[15px] font-semibold">{t("config")}</h2>
          <pre className="mono overflow-x-auto" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify({ services_file: settings.services_file, ...settings.config }, null, 2)}</pre>
        </section>
      )}
    </div>
  );
}
