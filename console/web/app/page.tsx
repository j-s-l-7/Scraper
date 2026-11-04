"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { Security, useApi } from "@/lib/api";
import { cr, date, fmt, isNum, pct, rs } from "@/lib/format";
import { Empty, ErrorBox, TableSkeleton } from "@/components/ui";

type Col = { k: keyof Security; label: string; num?: boolean };
const COLS: Col[] = [
  { k: "rank", label: "#", num: true }, { k: "symbol", label: "Symbol" }, { k: "name", label: "Company" }, { k: "sector", label: "Sector" },
  { k: "market_cap_cr", label: "Mkt cap", num: true }, { k: "price", label: "Price", num: true }, { k: "pe", label: "P/E", num: true },
  { k: "roe_pct", label: "ROE %", num: true }, { k: "roce_pct", label: "ROCE %", num: true }, { k: "ret_1y", label: "1Y %", num: true },
  { k: "complete_pct", label: "Complete", num: true },
];

export default function Securities() {
  const { data, error, loading } = useApi<{ items: Security[]; as_of: string | null }>("/api/securities");
  const [q, setQ] = useState("");
  const [sector, setSector] = useState("");
  const [sort, setSort] = useState<{ k: keyof Security; desc: boolean }>({ k: "rank", desc: false });
  const [sel, setSel] = useState(0);
  const router = useRouter();
  const rowsRef = useRef<Security[]>([]);

  const rows = useMemo(() => {
    const items = data?.items ?? [];
    const needle = q.trim().toLowerCase();
    const f = items.filter((r) => (!sector || r.sector === sector) && (!needle || r.symbol.toLowerCase().includes(needle) || r.name.toLowerCase().includes(needle) || (r.sector ?? "").toLowerCase().includes(needle)));
    return [...f].sort((a, b) => {
      const x = a[sort.k], y = b[sort.k];
      if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1;
      const c = typeof x === "string" ? x.localeCompare(y as string) : (x as number) - (y as number);
      return sort.desc ? -c : c;
    });
  }, [data, q, sector, sort]);
  rowsRef.current = rows;
  const sectors = useMemo(() => [...new Set((data?.items ?? []).map((r) => r.sector).filter(Boolean))].sort() as string[], [data]);
  const total = useMemo(() => rows.reduce((s, r) => s + (r.market_cap_cr ?? 0), 0), [rows]);

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.tagName === "INPUT" || t.tagName === "SELECT" || e.metaKey || e.ctrlKey) return;
      if (e.key === "j") setSel((s) => Math.min(rowsRef.current.length - 1, s + 1));
      else if (e.key === "k") setSel((s) => Math.max(0, s - 1));
      else if (e.key === "Enter" && rowsRef.current[sel]) router.push(`/company/?s=${rowsRef.current[sel].symbol}`);
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [router, sel]);
  useEffect(() => { document.querySelector(".tbl tr.sel")?.scrollIntoView({ block: "nearest" }); }, [sel]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="mr-2 text-[17px] font-semibold">Securities</h1>
        <input data-search className="input w-64" placeholder="Search symbol, name or sector   ( / )" value={q} onChange={(e) => { setQ(e.target.value); setSel(0); }} />
        <select className="input" value={sector} onChange={(e) => setSector(e.target.value)} aria-label="Sector">
          <option value="">All sectors</option>{sectors.map((s) => <option key={s}>{s}</option>)}
        </select>
        <span className="ml-auto text-muted tabular-nums">{rows.length} companies · {cr(total)} · as of {date(data?.as_of)}</span>
      </div>
      {error && <ErrorBox error={error} />}
      {loading && !data && <TableSkeleton rows={14} />}
      {data && rows.length === 0 && <Empty title="No companies match" hint="Clear the search or sector filter." />}
      {data && rows.length > 0 && (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                {COLS.map((c, i) => (
                  <th key={c.k} className={`${c.num ? "" : "l"} ${i === 1 ? "stick" : ""} cursor-pointer`} onClick={() => setSort((s) => ({ k: c.k, desc: s.k === c.k ? !s.desc : c.num === true && c.k !== "rank" }))}>
                    {c.label}{sort.k === c.k ? (sort.desc ? " ↓" : " ↑") : ""}
                  </th>
                ))}
                <th className="l">Coverage</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.symbol} className={i === sel ? "sel" : ""} onClick={() => router.push(`/company/?s=${r.symbol}`)} style={{ cursor: "pointer" }}>
                  <td className="num">{r.rank}</td>
                  <td className="l stick font-medium"><Link href={`/company/?s=${r.symbol}`} className="text-brand hover:underline">{r.symbol}</Link></td>
                  <td className="l max-w-[280px] truncate">{r.name}</td>
                  <td className="l text-muted">{r.sector ?? "–"}</td>
                  <td className="num">{cr(r.market_cap_cr)}</td>
                  <td className="num">{rs(r.price)}</td>
                  <td className="num">{fmt(r.pe, 1)}</td>
                  <td className="num">{fmt(r.roe_pct, 1)}</td>
                  <td className="num">{fmt(r.roce_pct, 1)}</td>
                  <td className={`num ${isNum(r.ret_1y) && r.ret_1y < 0 ? "neg" : ""}`}>{pct(r.ret_1y, 1)}</td>
                  <td className={`num ${isNum(r.complete_pct) && r.complete_pct < 95 ? "text-warn" : ""}`}
                    title={isNum(r.complete_pct) ? `accounted ${r.complete_pct}% (present ${r.covered_pct}%) · ${r.open_gaps ?? 0} open items` : "ledger not built"}>
                    {isNum(r.complete_pct) ? `${r.complete_pct.toFixed(r.complete_pct >= 99.95 ? 0 : 1)}%` : "–"}</td>
                  <td className="l">
                    <span className="flex gap-1">
                      <span className={`pill ${r.has_screener ? "ok" : "bad"}`} title="screener.in financials">scr</span>
                      <span className={`pill ${r.has_yf ? "ok" : "bad"}`} title="yfinance data">yf</span>
                      <span className={`pill ${r.ar_n ? "ok" : "warn"}`} title="annual reports with text">AR {r.ar_n}</span>
                      <span className={`pill ${r.cc_n ? "ok" : "warn"}`} title="concall transcripts with text">CC {r.cc_n}</span>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
