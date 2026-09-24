import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import { initialLang, makeT, saveLang } from "./i18n.js";
import Panel from "./pages/Panel.jsx";
import Incidents from "./pages/Incidents.jsx";
import Gpu from "./pages/Gpu.jsx";
import Logs from "./pages/Logs.jsx";
import Settings from "./pages/Settings.jsx";

const PAGES = [
  { path: "panel", key: "nav_panel", icon: "M3 12h4l3-8 4 16 3-8h4", component: Panel },
  { path: "incidents", key: "nav_incidents", icon: "M12 3l9 16H3zM12 10v4M12 17h.01", component: Incidents },
  { path: "gpu", key: "nav_gpu", icon: "M4 7h16v10H4zM8 7V4M16 7V4M8 20v-3M16 20v-3", component: Gpu },
  { path: "logs", key: "nav_logs", icon: "M5 5h14M5 10h14M5 15h9M5 20h6", component: Logs },
  { path: "services", key: "nav_services", icon: "M12 15a3 3 0 100-6 3 3 0 000 6zM19 12l2-1-1-3-2 .3-1.4-1.4.3-2-3-1-1 2h-2l-1-2-3 1 .3 2L6.8 7.3 5 7 4 10l2 1v2l-2 1 1 3 2-.3 1.4 1.4-.3 2 3 1 1-2h2l1 2 3-1-.3-2 1.4-1.4 2 .3 1-3-2-1z", component: Settings },
];

const AppContext = createContext(null);
export const useApp = () => useContext(AppContext);

function useHashRoute() {
  const read = () => {
    const [path, qs] = window.location.hash.replace(/^#\/?/, "").split("?");
    const parts = path.split("/");
    return { page: parts[0] || "panel", param: parts[1] || null, query: new URLSearchParams(qs || "") };
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onChange = () => setRoute(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function Icon({ d, size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

export function Toast({ message, onClose }) {
  useEffect(() => {
    if (!message) return undefined;
    const timer = setTimeout(onClose, 4500);
    return () => clearTimeout(timer);
  }, [message, onClose]);
  if (!message) return null;
  return (
    <div className="toast" role="status" onClick={onClose}>
      {message}
    </div>
  );
}

export default function App() {
  const route = useHashRoute();
  const [lang, setLang] = useState(initialLang);
  const t = useMemo(() => makeT(lang), [lang]);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [toast, setToast] = useState(null);

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.status());
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  const act = useCallback(
    async (fn, okMessage) => {
      try {
        const result = await fn();
        const message = typeof okMessage === "function" ? okMessage(result) : okMessage;
        if (message) setToast(message);
        await refresh();
        return result;
      } catch (e) {
        setToast(e.message);
        return null;
      }
    },
    [refresh],
  );
  const notify = useCallback((message) => setToast(message), []);
  const value = useMemo(() => ({ status, refresh, act, notify, t, lang }), [status, refresh, act, notify, t, lang]);

  const page = PAGES.find((p) => p.path === route.page) || PAGES[0];
  const Component = page.component;
  const openIncidents = status?.incidents?.open || 0;

  const switchLang = () => {
    const next = lang === "es" ? "en" : "es";
    saveLang(next);
    setLang(next);
  };

  return (
    <AppContext.Provider value={value}>
      <div className="min-h-dvh md:grid md:grid-cols-[216px_minmax(0,1fr)]">
        <aside className="sticky top-0 z-10 border-b md:self-start md:h-dvh md:border-b-0 md:border-r" style={{ background: "var(--sidebar)", borderColor: "var(--line)" }}>
          <div className="flex items-center gap-3 px-4 py-3 md:px-5 md:py-5">
            <img src="/icon-192.png" alt="" width="34" height="34" className="rounded-lg" />
            <div className="leading-tight">
              <div className="text-[15px] font-semibold">Cassandra's Hoard</div>
              <div className="help text-[11px]">{t("tagline")}</div>
            </div>
          </div>
          <nav aria-label="Sections" className="flex gap-1 overflow-x-auto px-3 pb-2 md:flex-col">
            {PAGES.map((p) => (
              <a key={p.path} href={`#/${p.path}`} className="nav-link shrink-0 text-[13px]" aria-current={p.path === page.path ? "page" : undefined}>
                <Icon d={p.icon} />
                {t(p.key)}
                {p.path === "incidents" && openIncidents > 0 && (
                  <span className="chip ml-auto" style={{ background: "var(--danger-bg)", color: "#ffb4ae" }}>{openIncidents}</span>
                )}
              </a>
            ))}
          </nav>
          <div className="hidden px-5 pt-4 md:block">
            {status && (
              <>
                <div className="help text-[11px]">{t("last_check")}</div>
                <div className="text-[12px]">
                  {status.poller.last_tick ? new Date(status.poller.last_tick).toLocaleTimeString() : "—"} · {t("every")} {status.poller.interval_s} s
                </div>
              </>
            )}
            <button type="button" className="btn btn-sm mt-4" onClick={switchLang}>{t("language")}</button>
          </div>
        </aside>
        <main className="min-w-0 px-4 py-4 md:px-8 md:py-7">
          {error && (
            <div className="mb-4 rounded-md border p-3 text-[13px]" style={{ background: "var(--danger-bg)", borderColor: "#e5534b66" }} role="alert">
              {t("unreachable")}: {error}. <button type="button" className="btn-link" onClick={refresh}>{t("retry")}</button>
            </div>
          )}
          <Component param={route.param} query={route.query} />
          <div className="mt-8 md:hidden">
            <button type="button" className="btn btn-sm" onClick={switchLang}>{t("language")}</button>
          </div>
        </main>
      </div>
      <Toast message={toast} onClose={() => setToast(null)} />
    </AppContext.Provider>
  );
}
