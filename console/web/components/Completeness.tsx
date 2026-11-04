"use client";
import { Fragment, useState } from "react";
import { useApi } from "@/lib/api";
import { date, fmt } from "@/lib/format";
import { ErrorBox, Skeleton } from "@/components/ui";

// Shapes of /api/completeness and /api/securities/{sym}/gaps (equity.data_gap, equity.completeness[_daily]).
export type DatasetMeta = { dataset: string; label: string; unit: string; chain: string[] };
export type ReasonCat = Record<string, { state: "open" | "explained"; label: string }>;
type Counts = { expected: number; present: number; explained: number; open: number };
export type Gap = { dataset: string; period: string; state: "open" | "explained"; reason: string; detail: Record<string, unknown>; next_source: string | null; first_seen: string };
type CompResp = { as_of: string | null; summary: (DatasetMeta & Counts & { companies: number; reasons: Record<string, number> })[]; reasons: ReasonCat; error?: string };
type GapsResp = { datasets: (DatasetMeta & Counts & { note: Gap | null })[]; gaps: Gap[]; reasons: ReasonCat; error?: string };

const TONE = { present: "var(--accent)", explained: "var(--c5)", open: "var(--warn)" } as const;
const pctOf = (n: number, d: number) => (d > 0 ? (100 * n) / d : null);
const p1 = (v: number | null) => (v === null ? "–" : `${v >= 99.95 || v === 0 ? v.toFixed(0) : v.toFixed(1)}%`);
const chainText = (c: string[]) => c.map((s) => s.replace(":local", " (local)")).join(" → ");
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** '2025-06-30' -> 'Jun 2025' (quarter / period end); FY2024 and years pass through. */
const periodLabel = (p: string) => (/^\d{4}-\d{2}-\d{2}$/.test(p) ? `${MON[+p.slice(5, 7) - 1]} ${p.slice(0, 4)}` : p);
const detailText = (d: Record<string, unknown>) =>
  Object.entries(d ?? {}).map(([k, v]) => `${k.replace(/_/g, " ")}: ${Array.isArray(v) ? v.join(", ") : String(v)}`).join(" · ");

/** Present | explained | open as one bar: explained absences count as accounted for, open ones are actionable. */
export function SplitBar({ c, h = 8 }: { c: Counts; h?: number }) {
  const w = (n: number) => `${c.expected > 0 ? (100 * n) / c.expected : 0}%`;
  return (
    <div className="flex w-full overflow-hidden rounded-full bg-surface-2" style={{ height: h }}
      title={`${fmt(c.present, 0)} present · ${fmt(c.explained, 0)} absent with a reason · ${fmt(c.open, 0)} open of ${fmt(c.expected, 0)}`}>
      <div style={{ width: w(c.present), background: TONE.present }} />
      <div style={{ width: w(c.explained), background: TONE.explained }} />
      <div style={{ width: w(c.open), background: TONE.open }} />
    </div>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-3 text-[11.5px] text-muted">
      {([["present", "present"], ["explained", "absent, reason known"], ["open", "absent, next source pending"]] as const).map(([k, l]) => (
        <span key={k} className="inline-flex items-center gap-1"><span className="inline-block h-2 w-3 rounded-sm" style={{ background: TONE[k] }} />{l}</span>
      ))}
    </div>
  );
}

const ReasonPill = ({ code, cat }: { code: string; cat: ReasonCat }) => (
  <span className={`pill ${cat[code]?.state === "open" ? "warn" : ""}`} title={cat[code]?.label}>{code.replace(/_/g, " ")}</span>
);

/** Ops: universe-wide completeness by dataset, with the reasons behind every absent item and each source chain. */
export function CompletenessPanel() {
  const { data, error } = useApi<CompResp>("/api/completeness");
  const [open, setOpen] = useState<string | null>(null);
  if (error && !data) return <ErrorBox error={error} />;
  if (!data) return <Skeleton h={220} />;
  return (
    <section className="card space-y-2 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <b>Dataset completeness</b>
        <span className="text-[11.5px] text-muted">local lake · ledger of {date(data.as_of)} · <code>python -m screener_scraper gaps</code></span>
      </div>
      {data.error && <div className="text-[11.5px] text-bad">{data.error}</div>}
      {data.summary.length === 0 ? <div className="text-muted">The ledger has not been built yet.</div> : (
        <>
          <Legend />
          <div className="tbl-wrap"><table className="tbl">
            <thead><tr><th className="l">Dataset</th><th>Expected</th><th>Present</th><th>Explained</th><th>Open</th><th>Covered</th><th>Accounted</th><th className="l" style={{ minWidth: 160 }} /><th className="l">Main reasons</th></tr></thead>
            <tbody>{data.summary.map((s) => (
              <Fragment key={s.dataset}>
                <tr onClick={() => setOpen(open === s.dataset ? null : s.dataset)} style={{ cursor: "pointer" }}>
                  <td className="l"><span className="font-medium">{s.label}</span> <span className="text-[11px] text-muted">{s.unit}</span></td>
                  <td className="num">{fmt(s.expected, 0)}</td><td className="num">{fmt(s.present, 0)}</td>
                  <td className="num text-muted">{fmt(s.explained, 0)}</td><td className={`num ${s.open ? "text-warn" : ""}`}>{fmt(s.open, 0)}</td>
                  <td className="num">{p1(pctOf(s.present, s.expected))}</td><td className="num font-semibold">{p1(pctOf(s.present + s.explained, s.expected))}</td>
                  <td className="l"><SplitBar c={s} /></td>
                  <td className="l"><span className="flex flex-wrap gap-1">{Object.entries(s.reasons).slice(0, 3).map(([r, n]) => (
                    <span key={r} className="inline-flex items-center gap-1"><ReasonPill code={r} cat={data.reasons} /><span className="tabular-nums text-[11px] text-muted">{fmt(n, 0)}</span></span>))}</span></td>
                </tr>
                {open === s.dataset && (
                  <tr><td className="l" colSpan={9}>
                    <div className="space-y-1 py-1 text-[12px]">
                      <div><span className="text-muted">Source chain:</span> {chainText(s.chain)}</div>
                      <table className="tbl"><tbody>{Object.entries(s.reasons).map(([r, n]) => (
                        <tr key={r}><td className="l"><ReasonPill code={r} cat={data.reasons} /></td><td className="l">{data.reasons[r]?.label}</td><td className="num">{fmt(n, 0)}</td></tr>))}</tbody></table>
                    </div>
                  </td></tr>
                )}
              </Fragment>
            ))}</tbody>
          </table></div>
          <div className="text-[11.5px] text-muted">Only items that should exist are counted: years before listing and results not yet due are outside the expectation. Covered = present / expected; accounted = (present + explained) / expected.</div>
        </>
      )}
    </section>
  );
}

/** Company data-quality tab: completeness per dataset and every absent item with its reason and next source. */
export function CompanyCompleteness({ sym }: { sym: string }) {
  const { data, error } = useApi<GapsResp>(`/api/securities/${sym}/gaps`);
  const [show, setShow] = useState<"open" | "all">("open");
  if (error && !data) return <ErrorBox error={error} />;
  if (!data) return <Skeleton h={200} />;
  if (data.error) return <div className="card p-3 text-[12px] text-bad">Completeness ledger unavailable: {data.error}</div>;
  const gaps = data.gaps.filter((g) => show === "all" || g.state === "open");
  const meta = Object.fromEntries(data.datasets.map((d) => [d.dataset, d]));
  return (
    <section className="card space-y-3 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2"><b>Completeness</b><Legend /></div>
      <table className="tbl"><tbody>{data.datasets.map((d) => (
        <tr key={d.dataset}>
          <td className="l"><span className="font-medium">{d.label}</span> <span className="text-[11px] text-muted">{d.unit}</span></td>
          <td className="num tabular-nums">{d.expected ? `${fmt(d.present, 0)} / ${fmt(d.expected, 0)}` : "–"}</td>
          <td className="l" style={{ minWidth: 140 }}>{d.expected > 0 && <SplitBar c={d} />}</td>
          <td className="num">{d.expected > 0 ? p1(pctOf(d.present + d.explained, d.expected)) : ""}</td>
          <td className="l text-[12px]">{d.note ? <span className="inline-flex flex-wrap items-center gap-1"><ReasonPill code={d.note.reason} cat={data.reasons} /><span className="text-muted">{data.reasons[d.note.reason]?.label}{Object.keys(d.note.detail ?? {}).length ? ` (${detailText(d.note.detail)})` : ""}</span></span>
            : d.open ? <span className="text-warn">{fmt(d.open, 0)} open</span> : d.expected ? <span className="text-muted">nothing actionable</span> : <span className="text-muted">none expected</span>}</td>
        </tr>))}</tbody></table>
      <div className="flex items-center gap-2">
        <b>Absent items</b>
        <button className="btn" aria-pressed={show === "open"} onClick={() => setShow("open")}>Open ({data.gaps.filter((g) => g.state === "open").length})</button>
        <button className="btn" aria-pressed={show === "all"} onClick={() => setShow("all")}>All ({data.gaps.length})</button>
      </div>
      {gaps.length === 0 ? <div className="text-muted">{show === "open" ? "Nothing open: every absent item has a known reason." : "Nothing absent."}</div> : (
        <div className="tbl-wrap" style={{ maxHeight: 420 }}><table className="tbl">
          <thead><tr><th className="l">Dataset</th><th className="l">Period</th><th className="l">Reason</th><th className="l">Next source</th><th className="l">Evidence</th></tr></thead>
          <tbody>{gaps.map((g) => (
            <tr key={g.dataset + g.period}>
              <td className="l">{meta[g.dataset]?.label ?? g.dataset}</td><td className="l tabular-nums">{periodLabel(g.period)}</td>
              <td className="l"><span className="inline-flex items-center gap-1"><ReasonPill code={g.reason} cat={data.reasons} /><span className="text-[12px] text-muted">{data.reasons[g.reason]?.label}</span></span></td>
              <td className="l">{g.next_source?.replace(":local", " (local)") ?? "–"}</td>
              <td className="l max-w-[360px] truncate text-[12px] text-muted" title={detailText(g.detail)}>{detailText(g.detail)}</td>
            </tr>))}</tbody>
        </table></div>
      )}
    </section>
  );
}
