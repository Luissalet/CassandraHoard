import React from "react";
import { STATE_COLORS, clock } from "../format.js";

export function StatePill({ state, t }) {
  const color = STATE_COLORS[state] || STATE_COLORS.unknown;
  return (
    <span className="chip" style={{ color: state === "never_seen" || state === "unknown" ? "var(--muted)" : "var(--ink)" }}>
      <span className="dot" style={{ background: color }} />
      {t(`state_${state}`)}
    </span>
  );
}

// A 24 h (or any window) lane: coloured segments, grey stripes where Cassandra was not running, marks at reboots.
export function Lane({ segments, since, until, gaps = [], reboots = [], t, lang, height = 14 }) {
  const span = Math.max(1, until - since);
  const x = (ts) => ((Math.min(until, Math.max(since, ts)) - since) / span) * 1000;
  return (
    <svg className="lane" viewBox="0 0 1000 10" preserveAspectRatio="none" style={{ height }} role="img" aria-label="timeline">
      <defs>
        <pattern id="gap-stripes" width="6" height="10" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="3" height="10" fill="#ffffff14" />
        </pattern>
      </defs>
      {segments.map(([a, b, state], i) => (
        <rect key={i} x={x(a)} y="0" width={Math.max(0.8, x(b) - x(a))} height="10" fill={STATE_COLORS[state] || STATE_COLORS.unknown}>
          <title>{`${t(`state_${state}`)} · ${clock(a, lang)} → ${clock(b, lang)}`}</title>
        </rect>
      ))}
      {gaps.map(([a, b], i) => (
        <rect key={`g${i}`} x={x(a)} y="0" width={Math.max(0.8, x(b) - x(a))} height="10" fill="url(#gap-stripes)">
          <title>{`${t("gap")} · ${clock(a, lang)} → ${clock(b, lang)}`}</title>
        </rect>
      ))}
      {reboots.map((ts, i) => (
        <rect key={`r${i}`} x={x(ts) - 1} y="0" width="2.5" height="10" fill="var(--gold)">
          <title>{`${t("reboot")} · ${clock(ts, lang)}`}</title>
        </rect>
      ))}
    </svg>
  );
}

export function Empty({ children }) {
  return <div className="panel help text-center">{children}</div>;
}
