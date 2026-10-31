"use client";
import React, { useEffect, useRef, useState } from "react";

export type Series = { name: string; color: string; points: [string, number | null][]; dashed?: boolean };

function useWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [w, setW] = useState(640);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((e) => setW(Math.max(280, e[0].contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}
const t = (d: string) => Date.parse(d.length === 10 ? d + "T00:00:00Z" : d);
function niceTicks(lo: number, hi: number, n = 4): number[] {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(8));
  return out;
}

export function LineChart({ series, height = 240, yFmt = (v: number) => String(v), zeroBase = false }: { series: Series[]; height?: number; yFmt?: (v: number) => string; zeroBase?: boolean }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const M = { l: 52, r: 12, t: 10, b: 22 };
  const all = series.flatMap((s) => s.points.filter((p) => p[1] !== null).map((p) => [t(p[0]), p[1] as number]));
  if (!all.length) return <div ref={ref} className="grid h-24 place-items-center text-muted">No data</div>;
  const xs = all.map((a) => a[0]), ys = all.map((a) => a[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = zeroBase ? Math.min(0, ...ys) : Math.min(...ys), y1 = Math.max(...ys);
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.06; if (!zeroBase) y0 -= pad; y1 += pad;
  const X = (v: number) => M.l + ((v - x0) / (x1 - x0 || 1)) * (w - M.l - M.r);
  const Y = (v: number) => M.t + (1 - (v - y0) / (y1 - y0)) * (height - M.t - M.b);
  const path = (s: Series) => {
    let d = "", pen = false;
    for (const [dt, v] of s.points) { if (v === null) { pen = false; continue; } d += `${pen ? "L" : "M"}${X(t(dt)).toFixed(1)},${Y(v).toFixed(1)}`; pen = true; }
    return d;
  };
  const yt = niceTicks(y0, y1), years = new Set<number>();
  const xt: number[] = [];
  const span = (x1 - x0) / 86400000;
  for (let i = 0; i <= 5; i++) xt.push(x0 + ((x1 - x0) * i) / 5);
  const lab = (v: number) => { const d = new Date(v); return span > 900 ? String(d.getUTCFullYear()) : d.toLocaleDateString("en-IN", { month: "short", year: "2-digit", timeZone: "UTC" }); };
  void years;
  const base = series[0].points;
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const xv = x0 + ((e.clientX - r.left - M.l) / (w - M.l - M.r)) * (x1 - x0);
    let lo = 0, hi = base.length - 1;
    while (lo < hi) { const m = (lo + hi) >> 1; if (t(base[m][0]) < xv) lo = m + 1; else hi = m; }
    setHover(lo);
  };
  const hp = hover !== null ? base[hover] : null;
  return (
    <div ref={ref} className="relative">
      <svg width={w} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)} role="img">
        {yt.map((v) => (<g key={v}><line x1={M.l} x2={w - M.r} y1={Y(v)} y2={Y(v)} stroke="var(--line)" /><text x={M.l - 6} y={Y(v) + 3.5} textAnchor="end" className="chart-axis">{yFmt(v)}</text></g>))}
        {xt.map((v, i) => (<text key={i} x={X(v)} y={height - 6} textAnchor={i === 0 ? "start" : i === xt.length - 1 ? "end" : "middle"} className="chart-axis">{lab(v)}</text>))}
        {series.map((s) => (<path key={s.name} d={path(s)} fill="none" stroke={s.color} strokeWidth={1.6} strokeDasharray={s.dashed ? "4 3" : undefined} strokeLinejoin="round" />))}
        {hp && <line x1={X(t(hp[0]))} x2={X(t(hp[0]))} y1={M.t} y2={height - M.b} stroke="var(--muted)" strokeDasharray="2 3" />}
        {hp && series.map((s) => { const p = s.points[hover!]; return p && p[1] !== null && p[0] === hp[0] ? <circle key={s.name} cx={X(t(p[0]))} cy={Y(p[1])} r={3} fill={s.color} /> : null; })}
      </svg>
      {hp && (
        <div className="pointer-events-none absolute left-14 top-1 rounded-md border border-line bg-surface px-2 py-1 text-[11.5px] shadow">
          <b>{new Date(t(hp[0])).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" })}</b>
          {series.map((s) => { const p = s.points[hover!]; return p && p[1] !== null ? <span key={s.name} className="ml-3"><span style={{ color: s.color }}>●</span> {s.name} <b className="tabular-nums">{yFmt(p[1])}</b></span> : null; })}
        </div>
      )}
      <div className="mt-1 flex gap-4 pl-14 text-[11.5px] text-muted">{series.map((s) => <span key={s.name}><span style={{ color: s.color }}>●</span> {s.name}</span>)}</div>
    </div>
  );
}

/** Stacked bars per period (shareholding). */
export function StackedBars({ periods, layers, height = 240 }: { periods: string[]; layers: { name: string; color: string; values: (number | null)[] }[]; height?: number }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const M = { l: 36, r: 8, t: 8, b: 22 };
  const n = periods.length;
  if (!n) return <div ref={ref} className="text-muted">No data</div>;
  const bw = (w - M.l - M.r) / n;
  const Y = (v: number) => M.t + (1 - v / 100) * (height - M.t - M.b);
  return (
    <div ref={ref} className="relative">
      <svg width={w} height={height} onMouseLeave={() => setHover(null)}>
        {[0, 25, 50, 75, 100].map((v) => (<g key={v}><line x1={M.l} x2={w - M.r} y1={Y(v)} y2={Y(v)} stroke="var(--line)" /><text x={M.l - 5} y={Y(v) + 3.5} textAnchor="end" className="chart-axis">{v}%</text></g>))}
        {periods.map((p, i) => {
          let acc = 0;
          return (
            <g key={p} onMouseEnter={() => setHover(i)}>
              <rect x={M.l + i * bw} y={M.t} width={bw} height={height - M.t - M.b} fill="transparent" />
              {layers.map((l) => { const v = l.values[i] ?? 0; const y = Y(acc + v); const h = Y(acc) - y; acc += v; return <rect key={l.name} x={M.l + i * bw + bw * 0.12} y={y} width={bw * 0.76} height={Math.max(0, h)} fill={l.color} opacity={hover === null || hover === i ? 1 : 0.6} />; })}
              {(n <= 14 || i % Math.ceil(n / 12) === 0) && <text x={M.l + i * bw + bw / 2} y={height - 6} textAnchor="middle" className="chart-axis">{new Date(t(p)).toLocaleDateString("en-IN", { month: "short", year: "2-digit", timeZone: "UTC" })}</text>}
            </g>
          );
        })}
      </svg>
      {hover !== null && (
        <div className="pointer-events-none absolute right-2 top-1 rounded-md border border-line bg-surface px-2 py-1 text-[11.5px] shadow">
          <b>{periods[hover]}</b>{layers.map((l) => <span key={l.name} className="ml-3"><span style={{ color: l.color }}>●</span> {l.name} <b className="tabular-nums">{l.values[hover] ?? "–"}</b></span>)}
        </div>
      )}
      <div className="mt-1 flex flex-wrap gap-4 pl-9 text-[11.5px] text-muted">{layers.map((l) => <span key={l.name}><span style={{ color: l.color }}>●</span> {l.name}</span>)}</div>
    </div>
  );
}

export function Bars({ items, height = 160, fmt = (v: number) => String(v) }: { items: { label: string; value: number }[]; height?: number; fmt?: (v: number) => string }) {
  const [ref, w] = useWidth();
  const M = { l: 8, r: 8, t: 14, b: 20 };
  if (!items.length) return <div ref={ref} className="text-muted">No data</div>;
  const max = Math.max(...items.map((i) => i.value), 1e-9), bw = (w - M.l - M.r) / items.length;
  return (
    <div ref={ref}>
      <svg width={w} height={height}>
        {items.map((it, i) => { const h = (it.value / max) * (height - M.t - M.b); return (
          <g key={it.label}>
            <rect x={M.l + i * bw + bw * 0.15} y={height - M.b - h} width={bw * 0.7} height={h} fill="var(--c1)" rx={2} />
            {items.length <= 20 && <text x={M.l + i * bw + bw / 2} y={height - M.b - h - 3} textAnchor="middle" className="chart-axis">{fmt(it.value)}</text>}
            {(items.length <= 16 || i % 2 === 0) && <text x={M.l + i * bw + bw / 2} y={height - 6} textAnchor="middle" className="chart-axis">{it.label}</text>}
          </g>); })}
      </svg>
    </div>
  );
}
