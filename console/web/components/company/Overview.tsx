"use client";
import { useState } from "react";
import { useApi } from "@/lib/api";
import { cr, fmt, isNum, pct, rs } from "@/lib/format";
import { Empty, Seg, Skeleton, Stat } from "@/components/ui";
import { LineChart } from "@/components/Charts";
import type { Sec } from "./CompanyView";

type Prices = { points: [string, number, number | null][]; stats: { first: number; last: number; high: number; low: number; change_pct: number | null; from: string; to: string } | null };

export default function Overview({ sym, d }: { sym: string; d: Sec }) {
  const [range, setRange] = useState("1y");
  const pr = useApi<Prices>(`/api/securities/${sym}/prices?range=${range}`);
  const s = d.snapshot || {}, y = d.yf || {}, p = d.profile;
  const price = s.price as number, bv = s.book_value as number;
  const target = y.target_mean_price as number;
  const basis = p.has_consolidated ? "C" : "S";
  const measures = ["sales_growth", "profit_growth", "price_cagr", "roe"], horizons = ["10Y", "5Y", "3Y", "1Y", "TTM", "LAST"];
  const g = (m: string, h: string) => d.growth.find((x) => x.basis === basis && x.measure === m && x.horizon === h)?.value_pct ?? null;
  const hi = s.high_52w as number, lo = s.low_52w as number;
  const pos = isNum(price) && hi > lo ? Math.min(100, Math.max(0, ((price - lo) / (hi - lo)) * 100)) : null;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-6">
        <Stat label="Market cap" value={cr(s.market_cap_cr as number)} />
        <Stat label="P/E" value={fmt(s.pe as number, 1)} sub={isNum(y.forward_pe as number) ? `fwd ${fmt(y.forward_pe as number, 1)}` : undefined} />
        <Stat label="P/B" value={isNum(price) && isNum(bv) && bv ? fmt(price / bv, 2) : "–"} sub={`book ${rs(bv)}`} />
        <Stat label="ROE" value={pct(s.roe_pct as number)} />
        <Stat label="ROCE" value={pct(s.roce_pct as number)} />
        <Stat label="Dividend yield" value={pct(s.dividend_yield_pct as number, 2)} />
        <Stat label="52w range" value={`${fmt(lo, 0)} – ${fmt(hi, 0)}`} sub={pos !== null ? `${pos.toFixed(0)}% of range` : undefined} />
        <Stat label="Face value" value={rs(s.face_value as number)} />
        <Stat label="Analyst target" value={rs(target, 0)} sub={isNum(target) && isNum(price) ? `${pct((target / price - 1) * 100, 1, true)} · ${y.recommendation_key ?? ""} (${y.num_analyst_opinions ?? 0})` : undefined}
          tone={isNum(target) && isNum(price) ? (target > price ? "pos" : "bad") : undefined} />
        <Stat label="Beta" value={fmt(y.beta as number, 2)} />
        <Stat label="EV/EBITDA" value={fmt(y.ev_to_ebitda as number, 1)} />
        <Stat label="Employees" value={isNum(y.full_time_employees as number) ? fmt(y.full_time_employees as number, 0) : "–"} />
      </div>
      <section className="card p-3">
        <div className="mb-2 flex items-center gap-3">
          <b>Price</b>
          <Seg value={range} onChange={setRange} options={["1m", "6m", "1y", "5y", "max"].map((v) => ({ v, label: v }))} />
          {pr.data?.stats && <span className={`ml-auto tabular-nums ${(pr.data.stats.change_pct ?? 0) < 0 ? "neg" : "text-pos"}`}>{pct(pr.data.stats.change_pct, 1, true)} over range · high {fmt(pr.data.stats.high, 0)} · low {fmt(pr.data.stats.low, 0)}</span>}
        </div>
        {pr.loading && !pr.data ? <Skeleton h={240} /> : pr.data && pr.data.points.length ? (
          <LineChart height={250} yFmt={(v) => fmt(v, v < 100 ? 1 : 0)} series={[{ name: "Close", color: "var(--c1)", points: pr.data.points.map((p) => [p[0], p[1]]) }]} />
        ) : <Empty title="No price history" />}
      </section>
      <div className="grid gap-4 lg:grid-cols-[1fr_1fr]">
        <section className="card p-3">
          <b>Growth summary</b> <span className="text-muted">({basis === "C" ? "consolidated" : "standalone"}, %)</span>
          {d.growth.length ? (
            <table className="tbl mt-2"><thead><tr><th className="l">Measure</th>{horizons.map((h) => <th key={h}>{h}</th>)}</tr></thead>
              <tbody>{measures.map((m) => <tr key={m}><td className="l">{m.replace("_", " ")}</td>{horizons.map((h) => { const v = g(m, h); return <td key={h} className={`num ${isNum(v) && v < 0 ? "neg" : ""}`}>{fmt(v, 1)}</td>; })}</tr>)}</tbody></table>
          ) : <Empty title="No growth summary" />}
        </section>
        <section className="card p-3">
          <b>About</b>
          <p className="mt-1 whitespace-pre-line text-[13px] leading-relaxed">{p.about || (y.yf_summary as string) || "No description."}</p>
          {p.website && <a className="text-brand underline" href={p.website} target="_blank" rel="noreferrer">{p.website}</a>}
        </section>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <section className="card p-3"><b className="text-pos">Pros</b><ul className="mt-1 list-disc space-y-1 pl-5">{(p.pros ?? []).map((x, i) => <li key={i}>{x}</li>)}{!p.pros?.length && <li className="list-none text-muted">None listed</li>}</ul></section>
        <section className="card p-3"><b className="text-bad">Cons</b><ul className="mt-1 list-disc space-y-1 pl-5">{(p.cons ?? []).map((x, i) => <li key={i}>{x}</li>)}{!p.cons?.length && <li className="list-none text-muted">None listed</li>}</ul></section>
      </div>
    </div>
  );
}
