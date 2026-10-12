import { test } from "node:test";
import assert from "node:assert/strict";
import { activeFilters, addRefreshHandler, buildContext, changedFields, installCopilot, onTurnEnd, parseRoute, readViewStates, rebaseForm, setViewState } from "./src/copilot-context.js";

const services = {
  services: [
    { id: "faustus", name: "Faustus", state: "up", group: "faustus", port: 7000, kind: "http", restart: { enabled: true, max_per_hour: 3 } },
    { id: "whisper", name: "Whisper server", state: "down", group: "custom", port: 9000, kind: "http", restart: { enabled: false, max_per_hour: 0 } },
  ],
};
const incident = { id: 12, service: "whisper", name: "Whisper server", kind: "state", from_state: "up", to_state: "down", opened_at: 1760000000, closed_at: null, open: true, probable_cause: "Process exited" };
const sites = { sites: [{ id: "example.com", name: "Shop", url: "https://example.com/", state: "up", status: 200, latency_ms: 120, enabled: true, interval_min: 5, tls: { days_left: 40 }, domain: { days_left: 200 } }] };
const status = { gpu: { now: [{ gpu: 0, mem_used_mb: 4000, mem_total_mb: 24000, mem_pct: 17, util_pct: 3 }] } };
const deps = {
  getService: async (id) => { const s = services.services.find((x) => x.id === id); if (!s) throw new Error("Unknown service"); return s; },
  getServices: async () => services,
  getSites: async () => sites,
  getIncident: async (id) => { if (Number(id) !== 12) throw new Error("Incident not found."); return incident; },
  getStatus: async () => status,
};
const ids = (ctx) => ctx.active_objects.map((o) => `${o.kind}:${o.object_id}`);

test("route parsing: page, parameter and query", () => {
  assert.deepEqual([parseRoute("").page, parseRoute("").param], ["panel", null]);
  const r = parseRoute("#/incidents/12?x=1");
  assert.deepEqual([r.page, r.param, r.query.get("x")], ["incidents", "12", "1"]);
});

test("filters: only the ones with a value, and the changed fields of a form", () => {
  assert.deepEqual(activeFilters({ q: "", service: "faustus", failed: false, open_only: true, since: "24h" }), { service: "faustus", open_only: true, since: "24h" });
  assert.deepEqual(changedFields({ a: 1, b: "x" }, { a: 1, b: "" }), { b: "" });
  assert.deepEqual(rebaseForm({ name: "A", url: "u" }, { name: "Agent", url: "u" }, { name: "A", url: "typed" }), { name: "Agent", url: "typed" });
});

test("panel: the expanded service and the expanded site are real objects", async () => {
  const states = { panel: { open_service: "faustus" }, sites: { open: "example.com", editing: true } };
  const ctx = await buildContext({ hash: "", states, ...deps });
  assert.deepEqual(ids(ctx), ["service:faustus", "site:example.com"]);
  assert.equal(ctx.active_objects[0].state, "up");
  assert.equal(ctx.active_objects[0].port, 7000);
  assert.equal(ctx.active_objects[0].auto_restart, true);
  assert.equal(ctx.active_objects[1].cert_days_left, 40);
  assert.equal(ctx.view.page, "panel");
  assert.equal(ctx.view.object_id, "faustus");
  assert.equal(ctx.view.sites_editor_open, true);
});

test("incidents: the route parameter or the expanded card, plus the applied filters and the typed ones", async () => {
  const states = { incidents: { open: null }, filters: { applied: { since: "7d" }, draft: { service: "whisper", since: "7d" } } };
  const byRoute = await buildContext({ hash: "#/incidents/12", states, ...deps });
  assert.deepEqual(ids(byRoute), ["incident:12"]);
  assert.equal(byRoute.active_objects[0].open, true);
  assert.equal(byRoute.active_objects[0].probable_cause, "Process exited");
  assert.deepEqual(byRoute.view.filters, { since: "7d" });
  assert.deepEqual(byRoute.view.editor, { kind: "filters-form", object_id: null, dirty: true, fields: { service: "whisper", since: "7d" } });
  const byCard = await buildContext({ hash: "#/incidents", states: { incidents: { open: 12 }, filters: { applied: {}, draft: null } }, ...deps });
  assert.deepEqual(ids(byCard), ["incident:12"]);
  assert.equal(byCard.view.editor, undefined);
});

test("gpu, logs and audit", async () => {
  const gpu = await buildContext({ hash: "#/gpu", states: { gpu: { hours: 6 } }, ...deps });
  assert.deepEqual(ids(gpu), ["gpu:0"]);
  assert.equal(gpu.view.range_hours, 6);
  const logs = await buildContext({ hash: "#/logs?service=whisper", states: { filters: { applied: { service: "whisper", since: "24h" }, draft: null } }, ...deps });
  assert.deepEqual(ids(logs), ["service:whisper"]);
  assert.deepEqual(logs.view.filters, { service: "whisper", since: "24h" });
  const audit = await buildContext({ hash: "#/audit", states: { filters: { applied: { type: "agent.call", since: "24h" }, draft: null } }, ...deps });
  assert.deepEqual(ids(audit), []);
  assert.equal(audit.view.filters.type, "agent.call");
});

test("services page: every watched service and the unsaved forms", async () => {
  const states = {
    "policy-field:whisper": { id: "whisper", fields: { max_per_hour: 5 } },
    "service-add": { fields: { id: "tts" } },
    "site-form:example.com": { id: "example.com", fields: { interval: "15" } },
    "site-add": { fields: { url: "https://x.example/" } },
  };
  const ctx = await buildContext({ hash: "#/services", states, ...deps });
  assert.deepEqual(ids(ctx), ["service:faustus", "service:whisper"]);
  assert.equal(ctx.active_objects[1].max_restarts_per_hour, 0);
  const kinds = ctx.view.editors.map((e) => e.kind).sort();
  assert.deepEqual(kinds, ["restart-policy-form", "service-new", "site-form", "site-new"]);
  assert.ok(ctx.view.editor.dirty);
  assert.equal(ctx.view.editors.find((e) => e.kind === "restart-policy-form").object_id, "whisper");
});

test("unknown ids, failing reads and 'new' never throw and yield no objects", async () => {
  const broken = { getService: async () => { throw new Error("down"); }, getServices: async () => { throw new Error("down"); }, getSites: async () => { throw new Error("down"); }, getIncident: async () => { throw new Error("down"); }, getStatus: async () => { throw new Error("down"); } };
  for (const hash of ["", "#/incidents/12", "#/incidents/new", "#/gpu", "#/logs", "#/audit", "#/services", "#/nope"]) {
    const ctx = await buildContext({ hash, states: { panel: { open_service: "faustus" }, sites: { open: "example.com" }, incidents: { open: 12 } }, ...broken });
    assert.deepEqual(ctx.active_objects, [], hash);
    assert.ok(ctx.view.page);
  }
  const missing = await buildContext({ hash: "#/incidents/999", states: {}, ...deps });
  assert.deepEqual(missing.active_objects, []);
  const fresh = await buildContext({ hash: "#/incidents/new", states: {}, ...deps });
  assert.deepEqual(fresh.active_objects, []);
  const none = await buildContext({ hash: "", states: { panel: { open_service: "ghost" } }, ...deps });
  assert.deepEqual(none.active_objects, []);
});

test("view states are a registry: set and clear", () => {
  setViewState("a", { x: 1 });
  assert.deepEqual(readViewStates().a, { x: 1 });
  setViewState("a", null);
  assert.equal(readViewStates().a, undefined);
});

test("turn end (event and callback) refreshes every registered view once", async () => {
  const target = new EventTarget();
  installCopilot(target);
  assert.equal(typeof target.hoardCopilot.context, "function");
  assert.equal(typeof target.hoardCopilot.onTurnEnd, "function");
  let a = 0;
  let b = 0;
  const offA = addRefreshHandler(() => { a += 1; });
  addRefreshHandler(() => { b += 1; });
  addRefreshHandler(() => { throw new Error("a broken view must not stop the others"); });
  target.dispatchEvent(new Event("hoard:copilot-turn-end"));
  onTurnEnd();
  await new Promise((r) => setTimeout(r, 400));
  assert.deepEqual([a, b], [1, 1]);
  offA();
  onTurnEnd();
  await new Promise((r) => setTimeout(r, 400));
  assert.deepEqual([a, b], [1, 2]);
});
