"use client";
import { Fragment, useMemo, useState } from "react";
import { FinRow, Financials as F, qs, useApi } from "@/lib/api";
import { byUnit, isNum, UNIT_LABEL } from "@/lib/format";
import { Empty, ErrorBox, Seg, TableSkeleton } from "@/components/ui";

const SEC_LABEL: Record<string, string> = { quarters: "Quarters", "profit-loss": "Profit & loss", "balance-sheet": "Balance sheet", "cash-flow": "Cash flow", ratios: "Ratios", "yf-income": "Income", "yf-balance": "Balance sheet", "yf-cashflow": "Cash flow" };
const PERIOD_LABEL: Record<string, string> = { A: "Annual", Q: "Quarterly", H: "Interim" };

export default function Financials({ sym, hasC }: { sym: string; hasC: boolean }) {
  const [source, setSource] = useState("screener");
  const [basis, setBasis] = useState(hasC ? "C" : "S");
  const [section, setSection] = useState("profit-loss");
  const [period, setPeriod] = useState("");
  const [open, setOpen] = useState<Set<number>>(new Set());
  const sections = source === "screener" ? ["quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios"] : ["yf-income", "yf-balance", "yf-cashflow"];
  const sec = sections.includes(section) ? section : sections[0];
  const { data, error, loading } = useApi<F>(`/api/securities/${sym}/financials${qs({ basis, source, section: sec, period })}`);

  const csv = () => {
    if (!data) return;
    const head = ["Line item", "Unit", ...data.columns.map((c) => c.label), ...(data.ttm ? ["TTM"] : [])];
    const lines: string[] = [head.join(",")];
    const walk = (r: FinRow, depth: number) => {
      lines.push([JSON.stringify("  ".repeat(depth) + r.name), r.unit, ...r.values.map((v) => v ?? ""), ...(data.ttm ? [r.ttm ?? ""] : [])].join(","));
      r.children.forEach((c) => walk(c, depth + 1));
    };
    data.rows.forEach((r) => walk(r, 0));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    a.download = `${sym}_${basis}_${sec}_${data.period}.csv`;
    a.click();
  };
  const allIds = useMemo(() => { const ids: number[] = []; const w = (r: FinRow) => { if (r.children.length) ids.push(r.metric_id); r.children.forEach(w); }; data?.rows.forEach(w); return ids; }, [data]);
  const toggle = (id: number) => setOpen((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const render = (r: FinRow, depth: number): React.ReactNode => {
    const has = r.children.length > 0, isOpen = open.has(r.metric_id);
    return (
      <Fragment key={r.metric_id}>
        <tr className={depth ? "child" : has ? "group" : ""}>
          <td className="l stick" style={{ minWidth: 230 }}>
            {has ? <button className="mr-1 inline-block w-4 text-muted" onClick={() => toggle(r.metric_id)} aria-label="Expand">{isOpen ? "▾" : "▸"}</button> : <span className="mr-1 inline-block w-4" />}
            {r.name} <span className="text-[10.5px] text-muted">{UNIT_LABEL[r.unit] ?? r.unit}</span>
          </td>
          {r.values.map((v, i) => <td key={i} className={`num ${isNum(v) && v < 0 ? "neg" : ""}`}>{byUnit(v, r.unit)}</td>)}
          {data?.ttm && <td className={`num ttm ${isNum(r.ttm) && r.ttm < 0 ? "neg" : ""}`}>{byUnit(r.ttm, r.unit)}</td>}
        </tr>
        {has && isOpen && r.children.map((c) => render(c, depth + 1))}
      </Fragment>
    );
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Seg value={source} onChange={(v) => { setSource(v); setSection(v === "screener" ? "profit-loss" : "yf-income"); setPeriod(""); }} options={[{ v: "screener", label: "screener.in" }, { v: "yfinance", label: "yfinance" }]} />
        <Seg value={basis} onChange={setBasis} options={[{ v: "C", label: "Consolidated" }, { v: "S", label: "Standalone" }]} />
        <Seg value={sec} onChange={(v) => { setSection(v); setPeriod(""); }} options={sections.map((v) => ({ v, label: SEC_LABEL[v] }))} />
        {data && data.available_periods.length > 1 && <Seg value={data.period} onChange={setPeriod} options={data.available_periods.map((v) => ({ v, label: PERIOD_LABEL[v] ?? v }))} />}
        <span className="ml-auto flex gap-2">
          {allIds.length > 0 && <button className="btn" onClick={() => setOpen(open.size ? new Set() : new Set(allIds))}>{open.size ? "Collapse all" : "Expand all"}</button>}
          <button className="btn" onClick={csv} disabled={!data?.rows.length}>Download CSV</button>
        </span>
      </div>
      {error && <ErrorBox error={error} />}
      {loading && !data && <TableSkeleton rows={12} />}
      {data && !data.rows.length && <Empty title="No data for this selection" hint={data.bases_available ? `Available bases: ${data.bases_available}. Try the other basis or source.` : "Try the other source or section."} />}
      {data && data.rows.length > 0 && (
        <>
          <div className="tbl-wrap">
            <table className="tbl">
              <thead><tr><th className="l stick">{SEC_LABEL[data.section]} · {data.basis === "C" ? "consolidated" : "standalone"}</th>{data.columns.map((c) => <th key={c.period_end}>{c.label}</th>)}{data.ttm && <th className="ttm">TTM</th>}</tr></thead>
              <tbody>{data.rows.map((r) => render(r, 0))}</tbody>
            </table>
          </div>
          <div className="text-[11.5px] text-muted">{data.units_note} Negative values in red. Source: {data.source}.</div>
        </>
      )}
    </div>
  );
}
