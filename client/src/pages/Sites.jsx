import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Lane, StatePill } from "../components/ui.jsx";
import { isoClock } from "../format.js";

const DASH = "\u2014";

// green above the warning threshold, amber at it, red at the danger one (the same days Cassandra warns at)
const tone = (days, warn, danger) =>
  days === null || days === undefined ? "var(--muted)" : days <= danger ? "var(--danger)" : days <= warn ? "var(--warn)" : "var(--ok)";

function CertCell({ tls }) {
  if (!tls) return <span className="help num" data-testid="site-cert-days">{DASH}</span>;
  const bad = tls.ok === false;
  const days = tls.days_left;
  const color = bad ? "var(--danger)" : tone(days, 21, 7);
  return (
    <span className="num" data-testid="site-cert-days" style={{ color }} title={tls.error || tls.issuer || ""}>
      {days === null || days === undefined ? (bad ? "!" : DASH) : `${days} d`}
    </span>
  );
}

function DomainCell({ domain }) {
  const days = domain?.days_left;
  return (
    <span className="num" data-testid="site-domain-days" style={{ color: tone(days, 30, 7) }} title={domain?.detail || domain?.registrar || ""}>
      {days === null || days === undefined ? DASH : `${days} d`}
    </span>
  );
}

function Detail({ site, t, lang }) {
  const tls = site.tls;
  const domain = site.domain;
  const dns = site.dns || {};
  return (
    <div className="space-y-1 px-3 pb-3 pt-2 text-[12px]" style={{ background: "var(--surface-2)" }} data-testid="site-detail">
      <div className="help">
        {site.url} {"\u00b7"} {t("site_every")} {site.interval_min} min {"\u00b7"} {t("site_expect")} {(site.expect_status || []).join(", ")}
      </div>
      {site.cause && site.state !== "up" && <div style={{ color: "var(--danger)" }}>{site.cause}</div>}
      {site.failing_checks ? <div style={{ color: "var(--warn)" }}>{t("site_failing", { n: site.failing_checks })}</div> : null}
      {site.redirect && <div>{t("site_redirect")}: {site.redirect}</div>}
      {site.keyword && (
        <div>
          {t("site_keyword")} "{site.keyword}": {site.keyword_ok === false ? t("site_keyword_missing") : site.keyword_ok ? t("site_keyword_found") : DASH}
        </div>
      )}
      <div>
        {t("site_cert")}: {tls ? (tls.error ? tls.error : `${t("site_valid_until")} ${isoClock(tls.not_after, lang)}${tls.issuer ? ` \u00b7 ${t("site_issuer")} ${tls.issuer}` : ""}`) : t("site_no_tls")}
      </div>
      <div>
        {t("site_domain")} {domain?.name || DASH}:{" "}
        {domain?.expires
          ? `${t("site_expires")} ${isoClock(domain.expires, lang)}${domain.registrar ? ` \u00b7 ${domain.registrar}` : ""}`
          : t("site_domain_unknown")}
      </div>
      {dns.addresses?.length > 0 && (
        <div className="help num">
          DNS {dns.addresses.join(", ")}
          {dns.changes ? ` \u00b7 ${t("site_dns_changed")} ${dns.changes}` : ""}
        </div>
      )}
      {site.last_change && (
        <div className="help">
          {t("site_last_change")}: {site.last_change.from} {"\u2192"} {site.last_change.to} {"\u00b7"} {isoClock(site.last_change.at, lang)}
        </div>
      )}
      <div className="help">
        {t("site_last_check")}: {isoClock(site.last_check, lang)}
        {site.open_incident && (
          <>
            {" \u00b7 "}
            <a href={`#/incidents/${site.open_incident}`}>{t("see_incident")} #{site.open_incident}</a>
          </>
        )}
      </div>
    </div>
  );
}
const parseExpect = (text) => text.split(/[\s,]+/).filter(Boolean);

function EditRow({ site, onSave, onRemove, t }) {
  const [form, setForm] = useState({
    name: site.name,
    url: site.url,
    expect: (site.expect_status || []).join(", "),
    keyword: site.keyword || "",
    interval: site.interval_min,
    enabled: site.enabled,
  });
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const save = () =>
    onSave({
      id: site.id,
      name: form.name,
      url: form.url,
      expect_status: parseExpect(form.expect),
      keyword: form.keyword,
      interval_min: Number(form.interval),
      enabled: form.enabled,
    });
  return (
    <div className="flex flex-wrap items-center gap-2 border-t px-3 py-2" style={{ borderColor: "var(--line)" }} data-testid="site-edit-row">
      <input className="field flex-[1_1_130px]" value={form.name} onChange={set("name")} aria-label={t("name")} />
      <input className="field mono flex-[2_1_220px]" value={form.url} onChange={set("url")} aria-label={t("url")} />
      <input className="field num w-[110px]" value={form.expect} onChange={set("expect")} aria-label={t("site_expect")} title={t("site_expect")} />
      <input className="field flex-[1_1_120px]" value={form.keyword} onChange={set("keyword")} placeholder={t("site_keyword")} aria-label={t("site_keyword")} />
      <input className="field num w-[80px]" type="number" min="1" max="1440" value={form.interval} onChange={set("interval")} aria-label={t("site_interval")} title={t("site_interval")} />
      <label className="flex items-center gap-1 text-[12px]">
        <input type="checkbox" checked={form.enabled} onChange={set("enabled")} /> {t("site_enabled")}
      </label>
      <div className="flex gap-2">
        <button type="button" className="btn btn-sm" onClick={save}>{t("save")}</button>
        <button type="button" className="btn btn-sm btn-danger" onClick={() => onRemove(site)}>{t("remove")}</button>
      </div>
    </div>
  );
}

function AddRow({ onAdd, t }) {
  const [form, setForm] = useState({ url: "", name: "", keyword: "", interval: "" });
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });
  const add = async () => {
    const body = { url: form.url.trim() };
    if (form.name.trim()) body.name = form.name.trim();
    if (form.keyword.trim()) body.keyword = form.keyword.trim();
    if (form.interval) body.interval_min = Number(form.interval);
    if (await onAdd(body)) setForm({ url: "", name: "", keyword: "", interval: "" });
  };
  return (
    <div className="flex flex-wrap items-center gap-2 border-t px-3 py-3" style={{ borderColor: "var(--line)" }} data-testid="site-add-row">
      <input className="field mono flex-[2_1_220px]" value={form.url} onChange={set("url")} placeholder="https://example.com/" aria-label={t("url")} />
      <input className="field flex-[1_1_130px]" value={form.name} onChange={set("name")} placeholder={t("name")} aria-label={t("name")} />
      <input className="field flex-[1_1_120px]" value={form.keyword} onChange={set("keyword")} placeholder={t("site_keyword")} aria-label={t("site_keyword")} />
      <input className="field num w-[80px]" type="number" min="1" max="1440" value={form.interval} onChange={set("interval")} placeholder="5" aria-label={t("site_interval")} title={t("site_interval")} />
      <button type="button" className="btn btn-sm btn-primary" disabled={!form.url.trim()} onClick={add}>{t("site_add")}</button>
    </div>
  );
}

export default function SitesCard() {
  const { act, t, lang } = useApp();
  const [data, setData] = useState(null);
  const [open, setOpen] = useState(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await api.sites(24));
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
    await act(() => api.checkSites(), t("sites_checked"));
    await load();
    setBusy(false);
  };
  const save = async (body) => {
    const result = await act(() => api.watchSite(body), t("saved"));
    await load();
    return result;
  };
  const remove = async (site) => {
    if (!window.confirm(t("site_remove_confirm", { name: site.name }))) return;
    await act(() => api.unwatchSite(site.id));
    await load();
  };

  if (!data || (!data.summary?.enabled && !data.sites.length)) return null;
  const sites = data.sites;
  const lanesWindow = { since: data.since, until: data.until };
  return (
    <section className="panel p-0" data-testid="sites-card">
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 pb-1 pt-3">
        <div>
          <h2 className="text-[13px] font-semibold" style={{ color: "var(--muted)" }}>{t("sites_title")}</h2>
          <div className="help text-[11px]">{t("sites_help")}</div>
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn btn-sm" onClick={checkNow} disabled={busy || !sites.length}>{busy ? t("checking") : t("sites_check")}</button>
          <button type="button" className="btn btn-sm" onClick={() => setEditing(!editing)} aria-pressed={editing}>{editing ? t("sites_done") : t("sites_edit")}</button>
        </div>
      </div>
      {data.error && <div className="px-3 pb-2 text-[12px]" style={{ color: "var(--warn)" }}>{data.error}</div>}
      {sites.length === 0 && <div className="help px-3 pb-3 text-[12px]">{t("sites_empty")} <span className="mono">{data.sites_file}</span></div>}
      {sites.map((site) => (
        <React.Fragment key={site.id}>
          <div className="site-row" data-testid="site-row" role="button" tabIndex={0} aria-expanded={open === site.id}
            onClick={() => setOpen(open === site.id ? null : site.id)} onKeyDown={(e) => e.key === "Enter" && setOpen(open === site.id ? null : site.id)}>
            <div className="min-w-0">
              <div className="truncate font-medium">{site.name}</div>
              <div className="help num truncate text-[11px]">
                {site.id}
                {site.lane?.uptime_pct !== null && site.lane?.uptime_pct !== undefined ? ` \u00b7 ${site.lane.uptime_pct}%` : ""}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-1">
              <StatePill state={site.state} t={t} />
              {site.status ? <span className="help num text-[11px]">{site.status}</span> : null}
            </div>
            <div className="num text-[12px]" title={t("latency")}>{site.latency_ms ? `${Math.round(site.latency_ms)} ms` : DASH}</div>
            <div className="text-[12px]" title={t("site_cert")}><CertCell tls={site.tls} /></div>
            <div className="text-[12px]" title={t("site_domain")}><DomainCell domain={site.domain} /></div>
            <div className="lane-cell">
              {site.lane ? <Lane segments={site.lane.segments} since={lanesWindow.since} until={lanesWindow.until} t={t} lang={lang} /> : <div className="lane" />}
            </div>
          </div>
          {open === site.id && <Detail site={site} t={t} lang={lang} />}
        </React.Fragment>
      ))}
      {editing && (
        <div data-testid="sites-editor">
          <div className="help px-3 pt-2 text-[11px]">{t("sites_file")}: <span className="mono">{data.sites_file}</span></div>
          {sites.map((site) => <EditRow key={`${site.id}:${site.url}:${site.interval_min}:${site.enabled}`} site={site} onSave={save} onRemove={remove} t={t} />)}
          <AddRow onAdd={save} t={t} />
        </div>
      )}
    </section>
  );
}