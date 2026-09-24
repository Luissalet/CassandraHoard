import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Empty } from "../components/ui.jsx";
import { clock, gb } from "../format.js";

const RANGES = [
  ["1h", 1],
  ["6h", 6],
  ["24h", 24],
  ["7d", 168],
];

// Plain SVG chart: memory (max, filled) and load (line) per bucket, 0-100 %.
function Chart({ gpu, since, until, t, lang }) {
  const W = 1000;
  const H = 180;
  const span = Math.max(1, until - since);
  const x = (ts) => ((ts - since) / span) * W;
  const y = (pct) => H - (Math.max(0, Math.min(100, pct)) / 100) * H;
  const series = gpu.series;
  const area = series.length
    ? `M${x(series[0][0])},${H} ` + series.map(([ts, mem]) => `L${x(ts)},${y(mem)}`).join(" ") + ` L${x(series[series.length - 1][0])},${H} Z`
    : "";
  const util = series.filter((p) => p[3] !== null).map(([ts, , , u], i) => `${i ? "L" : "M"}${x(ts)},${y(u)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => since + f * span);
  return (
    <svg viewBox={`0 0 ${W} ${H + 22}`} className="w-full" role="img" aria-label={`GPU ${gpu.gpu}`}>
      {[25, 50, 75, 100].map((p) => (
        <line key={p} x1="0" x2={W} y1={y(p)} y2={y(p)} stroke="#ffffff10" />
      ))}
      <path d={area} fill="var(--accent)" fillOpacity="0.35" stroke="var(--accent)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      {util && <path d={util} fill="none" stroke="var(--gold)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />}
      {gpu.peak_mem && <circle cx={x(gpu.peak_mem.ts)} cy={y(gpu.peak_mem.mem_pct)} r="4" fill="var(--danger)"><title>{`${t("peak")} ${gpu.peak_mem.mem_pct}% · ${clock(gpu.peak_mem.ts, lang)}`}</title></circle>}
      {ticks.map((ts, i) => (
        <text key={i} x={Math.min(W - 60, Math.max(0, x(ts) - 30))} y={H + 16} fill="var(--muted)" fontSize="12">{clock(ts, lang).slice(-8, -3)}</text>
      ))}
    </svg>
  );
}

export default function Gpu() {
  const { t, lang } = useApp();
  const [range, setRange] = useState(6);
  const [data, setData] = useState(null);
  const [window_, setWindow] = useState(null);

  const load = useCallback(async (hours) => {
    const until = Date.now() / 1000;
    const since = until - hours * 3600;
    try {
      setData(await api.gpu({ since: Math.floor(since), until: Math.ceil(until), points: 240 }));
      setWindow([since, until]);
    } catch {
      setData({ gpus: [] });
    }
  }, []);
  useEffect(() => {
    load(range);
    const timer = setInterval(() => load(range), 30000);
    return () => clearInterval(timer);
  }, [load, range]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-[22px] font-semibold">{t("nav_gpu")}</h1>
        <div className="flex gap-1" role="group" aria-label={t("range")}>
          {RANGES.map(([label, hours]) => (
            <button key={label} type="button" className={`btn btn-sm ${range === hours ? "btn-primary" : ""}`} aria-pressed={range === hours} onClick={() => setRange(hours)}>
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="help flex gap-4 text-[12px]">
        <span><span className="dot mr-1" style={{ background: "var(--accent)" }} />{t("mem")} (%)</span>
        <span><span className="dot mr-1" style={{ background: "var(--gold)" }} />{t("util")} (%)</span>
      </div>
      {data && data.gpus.length === 0 && <Empty>{t("no_gpu_samples")}</Empty>}
      {data && window_ && data.gpus.map((gpu) => (
        <section key={gpu.gpu} className="panel">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-[15px] font-semibold">GPU {gpu.gpu} <span className="help">· {gb(gpu.mem_total_mb)}</span></h2>
            {gpu.now && (
              <span className="num text-[12px]">
                {gb(gpu.now.mem_used_mb)} ({gpu.now.mem_pct}%) · {gb(gpu.now.mem_free_mb)} {t("free")} · {t("util")} {gpu.now.util_pct ?? "—"}%
              </span>
            )}
          </div>
          <Chart gpu={gpu} since={window_[0]} until={window_[1]} t={t} lang={lang} />
          <div className="help num mt-1 text-[12px]">
            {gpu.peak_mem && <>{t("peak")} {t("mem").toLowerCase()}: {gb(gpu.peak_mem.mem_used_mb)} ({gpu.peak_mem.mem_pct}%) {clock(gpu.peak_mem.ts, lang)}</>}
            {gpu.peak_util && <> · {t("peak")} {t("util").toLowerCase()}: {gpu.peak_util.util_pct}% {clock(gpu.peak_util.ts, lang)}</>}
          </div>
        </section>
      ))}
    </div>
  );
}
