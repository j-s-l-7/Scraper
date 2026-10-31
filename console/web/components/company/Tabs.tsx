"use client";
import Link from "next/link";
import { useState } from "react";
import { DocItem, useApi } from "@/lib/api";
import { byUnit, cr, date, DOC_LABEL, fmt, isNum, pct, rs } from "@/lib/format";
import { Bar, Empty, ErrorBox, Seg, Skeleton, Stat, StatusPill, TableSkeleton } from "@/components/ui";
import { Bars, LineChart, StackedBars } from "@/components/Charts";

function Load<T>({ url, children }: { url: string; children: (d: T) => React.ReactNode }) {
  const { data, error, loading } = useApi<T>(url);
  if (error) return <ErrorBox error={error} />;
  if (!data) return loading ? <TableSkeleton rows={8} /> : null;
  return <>{children(data)}</>;
}

// ---------------------------------------------------------------- Valuation
type Val = { basis: string; dates: string[]; series: Record<string, (number | null)[]>; summary: Record<string, { current: number; median: number; min: number; max: number; percentile: number }> };
const VMETA: [string, string, string, string][] = [["pe", "median_pe", "P/E", "x"], ["pbv", "median_pbv", "P/B", "x"], ["ev_multiple", "median_ev_multiple", "EV/EBITDA", "x"], ["mcap_sales", "median_mcap_sales", "Mcap/Sales", "x"]];
export function Valuation({ sym, hasC }: { sym: string; hasC: boolean }) {
  const [basis, setBasis] = useState(hasC ? "C" : "S");
  const [range, setRange] = useState("max");
  const [m, setM] = useState("pe");
  const meta = VMETA.find((v) => v[0] === m)!;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-3">
        <Seg value={m} onChange={setM} options={VMETA.map((v) => ({ v: v[0], label: v[2] }))} />
        <Seg value={range} onChange={setRange} options={["1y", "3y", "5y", "max"].map((v) => ({ v, label: v }))} />
        <Seg value={basis} onChange={setBasis} options={[{ v: "C", label: "Consolidated" }, { v: "S", label: "Standalone" }]} />
      </div>
      <Load<Val> url={`/api/securities/${sym}/valuation?basis=${basis}&range=${range}`}>{(d) => {
        const s = d.summary[m];
        const pts = (k: string): [string, number | null][] => d.dates.map((x, i) => [x, d.series[k]?.[i] ?? null]);
        return (
          <>
            {s && <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
              <Stat label={`${meta[2]} now`} value={fmt(s.current, 1)} /><Stat label="Median" value={fmt(s.median, 1)} />
              <Stat label="Range" value={`${fmt(s.min, 1)} – ${fmt(s.max, 1)}`} /><Stat label="Percentile" value={`${s.percentile}`} sub="of history below/at now" />
              <Stat label="Vs median" value={pct((s.current / s.median - 1) * 100, 0, true)} tone={s.current > s.median ? "bad" : "pos"} />
            </div>}
            <section className="card p-3"><b>{meta[2]} history</b> <span className="text-muted">({d.basis === "C" ? "consolidated" : "standalone"}; median line is screener's)</span>
              {d.series[m] ? <LineChart height={280} yFmt={(v) => fmt(v, 1)} series={[{ name: meta[2], color: "var(--c1)", points: pts(m) }, ...(d.series[meta[1]] ? [{ name: "Median", color: "var(--c2)", dashed: true, points: pts(meta[1]) }] : [])]} /> : <Empty title="No history for this metric" />}
            </section>
            {(d.series.opm || d.series.npm) && <section className="card p-3"><b>Margins %</b>
              <LineChart height={200} yFmt={(v) => fmt(v, 0)} series={(["opm", "npm", "gpm"] as const).filter((k) => d.series[k]).map((k, i) => ({ name: k.toUpperCase(), color: `var(--c${i + 1})`, points: pts(k) }))} /></section>}
          </>
        );
      }}</Load>
    </div>
  );
}

// ---------------------------------------------------------------- Shareholding
type SH = { periods: string[]; categories: string[]; series: Record<string, (number | null)[]>; num_shareholders: (number | null)[] | null; holders: { name: string; category: string; pct: number; prev_pct: number | null }[]; holders_as_of: string | null };
const CAT_LABEL: Record<string, string> = { promoters: "Promoters", fii: "FIIs", dii: "DIIs", government: "Government", public: "Public", others: "Others" };
export function Shareholding({ sym }: { sym: string }) {
  const [f, setF] = useState("Q");
  return (
    <div className="space-y-3">
      <Seg value={f} onChange={setF} options={[{ v: "Q", label: "Quarterly" }, { v: "Y", label: "Yearly" }]} />
      <Load<SH> url={`/api/securities/${sym}/shareholding?frequency=${f}`}>{(d) => d.periods.length === 0 ? <Empty title="No shareholding data" /> : (
        <>
          <section className="card p-3"><b>Holding pattern %</b>
            <StackedBars periods={d.periods} layers={d.categories.map((c, i) => ({ name: CAT_LABEL[c] ?? c, color: `var(--c${i + 1})`, values: d.series[c] }))} /></section>
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="tbl-wrap" style={{ maxHeight: 420 }}><table className="tbl"><thead><tr><th className="l stick">Category %</th>{d.periods.slice(-8).map((p) => <th key={p}>{p.slice(0, 7)}</th>)}</tr></thead>
              <tbody>{d.categories.map((c) => <tr key={c}><td className="l stick">{CAT_LABEL[c] ?? c}</td>{d.series[c].slice(-8).map((v, i) => <td key={i} className="num">{fmt(v, 2)}</td>)}</tr>)}
                {d.num_shareholders && <tr><td className="l stick">Shareholders</td>{d.num_shareholders.slice(-8).map((v, i) => <td key={i} className="num">{fmt(v, 0)}</td>)}</tr>}</tbody></table></div>
            <div className="tbl-wrap" style={{ maxHeight: 420 }}><table className="tbl"><thead><tr><th className="l">Holder (as of {date(d.holders_as_of)})</th><th className="l">Category</th><th>%</th><th>Change</th></tr></thead>
              <tbody>{d.holders.map((h, i) => { const ch = isNum(h.prev_pct) ? h.pct - h.prev_pct : null; return <tr key={i}><td className="l">{h.name}</td><td className="l text-muted">{CAT_LABEL[h.category] ?? h.category}</td><td className="num">{fmt(h.pct, 2)}</td><td className={`num ${isNum(ch) && ch < 0 ? "neg" : ""}`}>{isNum(ch) ? (ch > 0 ? "+" : "") + ch.toFixed(2) : "–"}</td></tr>; })}</tbody></table></div>
          </div>
        </>)}</Load>
    </div>
  );
}

// ---------------------------------------------------------------- Actions & estimates
type Act = { actions: { action_type: string; ex_date: string; value: number | null; detail: string | null; source: string }[]; dividends_by_year: { year: string; per_share: number }[]; shares: [string, number][] };
type Est = { as_of: string | null; estimates: Record<string, Record<string, Record<string, number | null>>>; earnings: { event_date: string; eps_estimate: number | null; eps_reported: number | null; surprise_pct: number | null }[]; targets: Record<string, number | string | null> | null };
export function ActionsEstimates({ sym, price }: { sym: string; price: number }) {
  return (
    <div className="space-y-4">
      <Load<Est> url={`/api/securities/${sym}/estimates`}>{(e) => {
        const t = e.targets || {};
        const per = (type: string) => Object.entries(e.estimates[type] || {});
        return (
          <>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
              <Stat label="Target mean" value={rs(t.target_mean_price as number, 0)} sub={isNum(t.target_mean_price as number) && isNum(price) ? pct(((t.target_mean_price as number) / price - 1) * 100, 1, true) : undefined} />
              <Stat label="Target range" value={`${fmt(t.target_low_price as number, 0)} – ${fmt(t.target_high_price as number, 0)}`} />
              <Stat label="Recommendation" value={String(t.recommendation_key ?? "–")} sub={`mean ${fmt(t.recommendation_mean as number, 2)}`} />
              <Stat label="Analysts" value={fmt(t.num_analyst_opinions as number, 0)} /><Stat label="Forward EPS" value={rs(t.forward_eps as number)} />
            </div>
            <div className="grid gap-4 lg:grid-cols-2">
              {(["eps", "revenue"] as const).map((type) => (
                <section key={type} className="card p-3"><b>{type === "eps" ? "EPS estimates (₹)" : "Revenue estimates (₹ Cr)"}</b> <span className="text-muted">as of {date(e.as_of)}</span>
                  {per(type).length === 0 ? <div className="mt-2 text-muted">None</div> : <table className="tbl mt-2"><thead><tr><th className="l">Period</th><th>Avg</th><th>Low</th><th>High</th><th>Growth</th><th>#</th></tr></thead>
                    <tbody>{per(type).map(([p, f]) => <tr key={p}><td className="l">{p}</td><td className="num">{fmt(f.avg, type === "eps" ? 2 : 0)}</td><td className="num">{fmt(f.low, type === "eps" ? 2 : 0)}</td><td className="num">{fmt(f.high, type === "eps" ? 2 : 0)}</td><td className="num">{isNum(f.growth) ? pct(f.growth * 100, 1) : "–"}</td><td className="num">{fmt(f.numberOfAnalysts, 0)}</td></tr>)}</tbody></table>}
                </section>))}
            </div>
            <section className="card p-3"><b>Earnings events</b>
              {e.earnings.length === 0 ? <div className="mt-2 text-muted">None</div> : <table className="tbl mt-2"><thead><tr><th className="l">Date</th><th>EPS est</th><th>EPS reported</th><th>Surprise</th></tr></thead>
                <tbody>{e.earnings.map((x) => <tr key={x.event_date}><td className="l">{date(x.event_date)}</td><td className="num">{fmt(x.eps_estimate)}</td><td className="num">{fmt(x.eps_reported)}</td><td className={`num ${isNum(x.surprise_pct) && x.surprise_pct < 0 ? "neg" : ""}`}>{pct(x.surprise_pct, 2)}</td></tr>)}</tbody></table>}</section>
          </>);
      }}</Load>
      <Load<Act> url={`/api/securities/${sym}/actions`}>{(a) => (
        <div className="grid gap-4 lg:grid-cols-2">
          <section className="card p-3"><b>Dividend per share by year (₹)</b>
            <Bars items={a.dividends_by_year.slice(-20).map((d) => ({ label: d.year.slice(2), value: d.per_share }))} fmt={(v) => fmt(v, v < 10 ? 1 : 0)} /></section>
          <section className="card p-3"><b>Shares outstanding</b>
            {a.shares.length > 1 ? <LineChart height={160} yFmt={(v) => (v / 1e7).toFixed(0) + " Cr"} series={[{ name: "Shares", color: "var(--c3)", points: a.shares }]} /> : <div className="text-muted">No data</div>}</section>
          <section className="card p-3 lg:col-span-2"><b>Corporate actions</b>
            {a.actions.length === 0 ? <div className="mt-2 text-muted">None</div> : <div className="tbl-wrap mt-2" style={{ maxHeight: 340 }}><table className="tbl"><thead><tr><th className="l">Ex-date</th><th className="l">Type</th><th>Value</th><th className="l">Detail</th><th className="l">Source</th></tr></thead>
              <tbody>{a.actions.map((x, i) => <tr key={i}><td className="l">{date(x.ex_date)}</td><td className="l">{x.action_type}</td><td className="num">{fmt(x.value, 2)}</td><td className="l text-muted">{x.detail ?? ""}</td><td className="l text-muted">{x.source}</td></tr>)}</tbody></table></div>}</section>
        </div>)}</Load>
    </div>
  );
}

// ---------------------------------------------------------------- Documents
export function Documents({ sym }: { sym: string }) {
  return (
    <Load<{ items: DocItem[]; status_counts: Record<string, number> }> url={`/api/securities/${sym}/documents`}>{(d) => {
      if (!d.items.length) return <Empty title="No documents cataloged" />;
      const types = [...new Set(d.items.map((i) => i.doc_type))];
      return (
        <div className="space-y-4">
          <div className="flex gap-2">{Object.entries(d.status_counts).map(([s, n]) => <span key={s}><StatusPill s={s} /> {n}</span>)}</div>
          {types.map((t) => (
            <section key={t}><b>{DOC_LABEL[t] ?? t}</b>
              <div className="tbl-wrap mt-1" style={{ maxHeight: 420 }}><table className="tbl"><thead><tr><th className="l stick">Period</th><th>FY</th><th className="l">Status</th><th>Pages</th><th className="l">Source</th><th className="l">Fetched via</th><th className="l">Library</th><th className="l"></th></tr></thead>
                <tbody>{d.items.filter((i) => i.doc_type === t).map((i) => (
                  <tr key={i.key}><td className="l stick">{i.period_label ?? "–"}</td><td className="num">{i.fiscal_year ?? "–"}</td><td className="l"><StatusPill s={i.status} /></td>
                    <td className="num">{fmt(i.page_count, 0)}</td><td className="l">{i.source ?? "–"}</td><td className="l">{i.fetch_source ?? "–"}</td>
                    <td className="l">{i.in_library ? <span className="pill ok">indexed</span> : i.has_file ? <span className="pill warn">file, not indexed</span> : <span className="pill">no file</span>}</td>
                    <td className="l">{i.has_file ? <Link className="text-brand underline" href={`/doc/?key=${i.key}`}>Open</Link> : null} <a className="ml-2 text-muted underline" href={i.url} target="_blank" rel="noreferrer">original</a></td></tr>))}</tbody></table></div>
            </section>))}
        </div>);
    }}</Load>
  );
}

// ---------------------------------------------------------------- Data quality
type Q = { coverage: Record<string, string | number | null>; completeness: Record<string, number | string[]> | null; tasks: { run_id: number; command: string; task: string; status: string; attempts: number; rows_written: number; error: string | null; started_at: string }[];
  missing_docs: { doc_type: string; period_label: string | null; url: string; status: string; error: string | null; fetch_source: string | null; attempts_log: { source?: string; url?: string; result?: string }[] }[] };
export function Quality({ sym }: { sym: string }) {
  return (
    <Load<Q> url={`/api/securities/${sym}/quality`}>{(q) => {
      const c = q.completeness as Record<string, number & string[]> | null;
      return (
        <div className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <section className="card p-3"><b>Source coverage</b>
              <table className="tbl mt-2"><tbody>{Object.entries(q.coverage || {}).filter(([k]) => !["company_id", "name"].includes(k)).map(([k, v]) => <tr key={k}><td className="l">{k}</td><td className="l tabular-nums">{String(v ?? "–")}</td></tr>)}</tbody></table></section>
            <section className="card space-y-2 p-3"><b>Document completeness</b>
              {c && ([["Annual reports", c.ar_found, c.ar_expected, c.ar_missing], ["Concall transcripts", c.cc_found, c.cc_expected, c.cc_missing]] as unknown as [string, number, number, string[]][]).map(([l, f, e, miss]) => (
                <div key={l}><div className="flex justify-between"><span>{l}</span><span className="tabular-nums text-muted">{f} / {e}</span></div><Bar value={f} max={e} />
                  {miss.length > 0 && <div className="mt-1 text-[11.5px] text-muted">missing: {miss.slice(0, 40).join(", ")}{miss.length > 40 ? " …" : ""}</div>}</div>))}
            </section>
          </div>
          <section className="card p-3"><b>Documents not ok ({q.missing_docs.length})</b>
            {q.missing_docs.length === 0 ? <div className="mt-2 text-muted">None, every cataloged document has text.</div> : q.missing_docs.map((m, i) => (
              <details key={i} className="mt-2 border-t border-line pt-2"><summary className="cursor-pointer"><StatusPill s={m.status} /> {DOC_LABEL[m.doc_type] ?? m.doc_type} · {m.period_label ?? "?"} {m.error && <span className="text-muted">· {m.error.slice(0, 100)}</span>}</summary>
                <div className="mt-1 break-all text-[12px]"><a className="text-brand underline" href={m.url} target="_blank" rel="noreferrer">{m.url}</a></div>
                {m.attempts_log?.length ? <table className="tbl mt-1"><tbody>{m.attempts_log.map((a, j) => <tr key={j}><td className="l">{a.source}</td><td className="l">{a.result}</td><td className="l max-w-[420px] truncate text-muted">{a.url}</td></tr>)}</tbody></table> : <div className="text-muted">No fallback attempts recorded.</div>}
              </details>))}
          </section>
          <section className="card p-3"><b>Scrape task trail</b>
            <div className="tbl-wrap mt-2" style={{ maxHeight: 360 }}><table className="tbl"><thead><tr><th className="l">Run</th><th className="l">Task</th><th className="l">Status</th><th>Attempts</th><th>Rows</th><th className="l">Error</th><th className="l">When</th></tr></thead>
              <tbody>{q.tasks.map((t, i) => <tr key={i}><td className="l">#{t.run_id} {t.command}</td><td className="l">{t.task}</td><td className="l"><StatusPill s={t.status} /></td><td className="num">{t.attempts}</td><td className="num">{t.rows_written}</td><td className="l max-w-[360px] truncate text-muted">{t.error ?? ""}</td><td className="l">{date(t.started_at)}</td></tr>)}</tbody></table></div></section>
        </div>);
    }}</Load>
  );
}
void byUnit; void cr; void Skeleton;
