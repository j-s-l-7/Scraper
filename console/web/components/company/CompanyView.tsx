"use client";
import { useApi } from "@/lib/api";
import { cr, fmt, pct, rs, date } from "@/lib/format";
import { ErrorBox, Skeleton } from "@/components/ui";
import Overview from "./Overview";
import Financials from "./Financials";
import { Valuation, Shareholding, ActionsEstimates, Documents, Quality } from "./Tabs";

const TABS = [
  ["overview", "Overview"], ["financials", "Financials"], ["valuation", "Valuation"], ["shareholding", "Shareholding"],
  ["actions", "Actions & Estimates"], ["documents", "Documents"], ["quality", "Data quality"],
] as const;

export type Sec = {
  profile: { symbol: string; name: string; sector: string | null; industry: string | null; industry_group: string | null; sub_industry: string | null; website: string | null;
    about: string | null; key_points: string | null; pros: string[] | null; cons: string[] | null; has_consolidated: boolean; isin: string | null; bse_code: string | null };
  universe: { rank: number; market_cap_cr: number } | null;
  snapshot: Record<string, number | string | null> | null;
  growth: { basis: string; measure: string; horizon: string; value_pct: number | null }[];
  yf: Record<string, number | string | null> | null;
};

export default function CompanyView({ sym, tab, setTab }: { sym: string; tab: string; setTab: (t: string) => void }) {
  const { data, error, loading } = useApi<Sec>(`/api/securities/${sym}`);
  if (error) return <ErrorBox error={error} />;
  if (loading && !data) return <div className="space-y-3"><Skeleton h={28} w={320} /><Skeleton h={90} /><Skeleton h={260} /></div>;
  if (!data) return null;
  const p = data.profile, s = data.snapshot || {};
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-x-4 gap-y-1">
        <h1 className="text-[20px] font-semibold tracking-tight">{p.symbol}</h1>
        <span className="text-[15px]">{p.name}</span>
        {p.sector && <span className="pill">{p.sector}</span>}
        {p.industry && <span className="pill">{p.industry}</span>}
        {data.universe && <span className="pill">rank #{data.universe.rank}</span>}
        <span className="ml-auto text-[18px] font-semibold tabular-nums">{rs(s.price as number)} <span className="text-[12px] font-normal text-muted">{cr(s.market_cap_cr as number)} · {date(s.as_of as string)}</span></span>
      </div>
      <div role="tablist" className="flex gap-1 overflow-x-auto border-b border-line">
        {TABS.map(([k, l]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}
            className={`-mb-px whitespace-nowrap border-b-2 px-3 py-1.5 text-[13px] ${tab === k ? "border-brand font-medium text-ink" : "border-transparent text-muted hover:text-ink"}`}>{l}</button>
        ))}
      </div>
      {tab === "overview" && <Overview sym={sym} d={data} />}
      {tab === "financials" && <Financials sym={sym} hasC={p.has_consolidated} />}
      {tab === "valuation" && <Valuation sym={sym} hasC={p.has_consolidated} />}
      {tab === "shareholding" && <Shareholding sym={sym} />}
      {tab === "actions" && <ActionsEstimates sym={sym} price={s.price as number} />}
      {tab === "documents" && <Documents sym={sym} />}
      {tab === "quality" && <Quality sym={sym} />}
    </div>
  );
}
void fmt; void pct;
