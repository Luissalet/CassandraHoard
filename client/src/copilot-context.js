// What the shared assistant panel asks this app for: the real objects open in the current view (an expanded service, a watched
// site, an incident, the GPUs, the filters of a search) and the unsaved form fields, plus a way to refresh the open view when the
// assistant finishes a turn. Nothing here renders anything.
import { api } from "./api.js";

const MAX_SERVICES = 12;
const FILTER_KEYS = ["q", "service", "level", "type", "source", "since", "at", "failed", "open_only"];

// ---- what each mounted view reports about itself ------------------------------------------------------------------------
const views = new Map();

// A view publishes a plain-data snapshot of what it shows (null removes it).
export function setViewState(key, snapshot) {
  if (snapshot === null || snapshot === undefined) views.delete(key);
  else views.set(key, snapshot);
}

export const readViewStates = (source = views) => Object.fromEntries(source);

export function parseRoute(hash) {
  const [path, query = ""] = String(hash || "").replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  return { page: parts[0] || "panel", param: parts[1] || null, query: new URLSearchParams(query) };
}

// Filter forms: only the keys that have a value (so an empty form is "no filter"); `failed`/`open_only` count when true.
export function activeFilters(form) {
  const out = {};
  for (const key of FILTER_KEYS) {
    const value = form?.[key];
    if (value === undefined || value === null || value === "" || value === false) continue;
    out[key] = value;
  }
  return out;
}

// Fields of `draft` that differ from `applied` (compared as JSON).
export function changedFields(initial, draft) {
  const base = initial || {};
  return Object.fromEntries(Object.entries(draft || {}).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(base[key])));
}

// A refreshed baseline arrives while the user is editing: fields the user did not touch follow the new baseline, touched ones stay.
export function rebaseForm(previousBase, nextBase, form) {
  const out = { ...nextBase };
  for (const key of Object.keys(form)) {
    if (JSON.stringify(form[key]) !== JSON.stringify(previousBase?.[key])) out[key] = form[key];
  }
  return out;
}

const pick = (source, keys) => Object.fromEntries(keys.filter((k) => source?.[k] !== undefined && source[k] !== null && source[k] !== "").map((k) => [k, source[k]]));

export const serviceObject = (s) => ({
  kind: "service",
  object_id: s.id,
  title: s.name || s.id,
  ...pick(s, ["state", "group", "port", "detail", "open_incident"]),
  ...(s.kind ? { service_kind: s.kind } : {}),
  ...(s.restart ? { auto_restart: Boolean(s.restart.enabled), max_restarts_per_hour: s.restart.max_per_hour } : {}),
});

export const siteObject = (s) => ({
  kind: "site",
  object_id: s.id,
  title: s.name || s.id,
  ...pick(s, ["url", "state", "status", "latency_ms", "open_incident", "enabled", "interval_min"]),
  ...(s.tls?.days_left !== undefined && s.tls?.days_left !== null ? { cert_days_left: s.tls.days_left } : {}),
  ...(s.domain?.days_left !== undefined && s.domain?.days_left !== null ? { domain_days_left: s.domain.days_left } : {}),
});

export const incidentObject = (i) => ({
  kind: "incident",
  object_id: String(i.id),
  title: i.name || i.service || String(i.id),
  ...pick(i, ["service", "opened_at", "closed_at", "probable_cause"]),
  incident_kind: i.kind || "state",
  open: Boolean(i.open),
  ...(i.from_state ? { from_state: i.from_state, to_state: i.to_state } : {}),
});

export const gpuObject = (g) => ({ kind: "gpu", object_id: String(g.gpu), title: `GPU ${g.gpu}`, ...pick(g, ["mem_used_mb", "mem_total_mb", "mem_pct", "util_pct"]) });

async function safe(fn) {
  try { return await fn(); } catch { return null; }
}

const formEditors = (states) => {
  const editors = [];
  for (const [key, snap] of Object.entries(states)) {
    if (!snap) continue;
    if (key.startsWith("site-form:")) editors.push({ kind: "site-form", object_id: snap.id, dirty: true, fields: snap.fields });
    else if (key === "site-add") editors.push({ kind: "site-new", object_id: null, dirty: true, fields: snap.fields });
    else if (key.startsWith("policy-field:")) editors.push({ kind: "restart-policy-form", object_id: snap.id, dirty: true, fields: snap.fields });
    else if (key === "service-add") editors.push({ kind: "service-new", object_id: null, dirty: true, fields: snap.fields });
    else if (key === "filters" && snap.draft) editors.push({ kind: "filters-form", object_id: null, dirty: true, fields: snap.draft });
  }
  return editors;
};

// Pure core (dependencies injected so it can be tested): never throws; unknown or removed ids yield no object.
export async function buildContext({ hash, states = {}, getService, getServices, getSites, getIncident, getStatus }) {
  const route = parseRoute(hash);
  const active = [];
  const seen = new Set();
  const add = (object) => { const key = `${object.kind}:${object.object_id}`; if (!seen.has(key)) { seen.add(key); active.push(object); } };
  const view = { page: route.page };
  const realId = (id) => (id && id !== "new" ? id : null);

  if (route.page === "panel") {
    const p = states.panel || {};
    if (realId(p.open_service)) {
      const svc = await safe(() => getService(p.open_service));
      if (svc?.id) { add(serviceObject(svc)); view.object_id = svc.id; }
    }
    const siteId = realId(states.sites?.open);
    if (siteId) {
      const sites = await safe(() => getSites());
      const site = (sites?.sites || []).find((s) => s.id === siteId);
      if (site) add(siteObject(site));
    }
    if (states.sites?.editing) view.sites_editor_open = true;
  } else if (route.page === "incidents") {
    const f = states.filters;
    const id = realId(route.param) || realId(states.incidents?.open);
    if (id) {
      const incident = await safe(() => getIncident(id));
      if (incident?.id !== undefined) { add(incidentObject(incident)); view.object_id = String(incident.id); }
    }
    if (f?.applied && Object.keys(f.applied).length) view.filters = f.applied;
  } else if (route.page === "gpu") {
    const status = await safe(() => getStatus());
    for (const g of status?.gpu?.now || []) add(gpuObject(g));
    if (states.gpu?.hours) view.range_hours = states.gpu.hours;
  } else if (route.page === "logs" || route.page === "audit") {
    const f = states.filters;
    if (f?.applied && Object.keys(f.applied).length) view.filters = f.applied;
    const serviceId = realId(f?.applied?.service);
    if (route.page === "logs" && serviceId) {
      const svc = await safe(() => getService(serviceId));
      if (svc?.id) add(serviceObject(svc));
    }
  } else if (route.page === "services") {
    const list = await safe(() => getServices());
    for (const s of (list?.services || []).slice(0, MAX_SERVICES)) if (s?.id) add(serviceObject(s));
  }

  const editors = formEditors(states);
  const editor = editors.find((e) => e.dirty);
  if (editor) view.editor = editor;
  if (editors.length > 1) view.editors = editors;
  return { active_objects: active, view };
}

export function context() {
  return buildContext({
    hash: globalThis.window?.location?.hash,
    states: readViewStates(),
    getService: (id) => api.service(id),
    getServices: () => api.services(),
    getSites: () => api.sites(24),
    getIncident: (id) => api.incident(id),
    getStatus: () => api.status(),
  });
}

// ---- refresh when the assistant finishes a turn -------------------------------------------------------------------------
const handlers = new Set();
let timer = null;

function scheduleRefresh() {
  clearTimeout(timer);
  timer = setTimeout(() => { for (const fn of [...handlers]) { try { fn(); } catch { /* a refresh must never break the page */ } } }, 250);
}

// A view registers what "reload what is on screen" means for it (it keeps typed form fields untouched).
export function addRefreshHandler(fn) {
  handlers.add(fn);
  return () => handlers.delete(fn);
}

export const onTurnEnd = () => scheduleRefresh();

// Once at start: the object the launcher looks for, and the window event it dispatches on turn end (the debounce merges the two).
export function installCopilot(target = globalThis.window) {
  if (!target || target.hoardCopilot) return;
  target.hoardCopilot = { context, onTurnEnd };
  target.addEventListener("hoard:copilot-turn-end", scheduleRefresh);
}
