// Small formatting helpers shared by the pages.
export const STATE_COLORS = {
  up: "var(--ok)",
  degraded: "var(--warn)",
  down: "var(--danger)",
  foreign: "var(--foreign)",
  never_seen: "var(--never)",
  unknown: "var(--never)",
};

export function clock(ts, lang) {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  const locale = lang === "en" ? "en-GB" : "es-ES";
  const time = date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return sameDay ? time : `${date.toLocaleDateString(locale, { day: "2-digit", month: "short" })} ${time}`;
}

export function isoClock(iso, lang) {
  return iso ? clock(Date.parse(iso) / 1000, lang) : "—";
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}

export function gb(mb) {
  return mb === null || mb === undefined ? "—" : `${(mb / 1024).toFixed(1)} GB`;
}
