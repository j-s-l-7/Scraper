"use client";
import { useEffect, useState } from "react";
import { useApi } from "@/lib/api";
import { bytes, dateTime, fmt } from "@/lib/format";
import { Bar, Empty, ErrorBox, Skeleton, StatusPill, TableSkeleton } from "@/components/ui";
import { CompletenessPanel } from "@/components/Completeness";

type Ops = {   // each section is absent when its loader failed with nothing cached: see `errors`
  errors: Record<string, string>;
  runs?: { run_id: number; command: string; status: string; stats: Record<string, unknown>; host: string | null; started_at: string; finished_at: string | null }[];
  jobs?: { kind: string; status: string; n: number }[];
  job_failures?: { kind: string; error_class: string; n: number }[];
  task_failures?: { task: string; error: string; n: number }[];
  db?: { bytes: number; limit_mb: number; cap_mb: number; tables: { name: string; bytes: number; est_rows: number }[] };
  documents?: { doc_type: string; status: string; n: number }[];
  completeness?: { ar_expected: number; ar_found: number; cc_expected: number; cc_found: number; ppt_expected: number; ppt_found: number; companies: number };
  completeness_gaps?: { symbol: string; ar_gap: number; cc_gap: number }[];
  control?: { key: string; value: unknown; reason: string | null }[];
  library: { built: boolean; docs: number; chunks: number; size_mb: number };
};

type Rollout = {
  bands?: { from: number; to: number; total: number; screener: number; yf: number; ar: number; cc: number }[];
  runs?: { run_id: number; parts_loaded: number; rows_loaded: number; complete: boolean; last_at: string | null }[];
  fleet: { name: string; alive: boolean; last: string; last_at: string | null; idle_min: number | null }[];
  fleet_paused: boolean;
  top100_primary: { value: string; updated_at: string } | null;
  dq?: { as_of: string; prev_as_of: string | null; companies: number; avg_overall: number; avg_coverage: number; avg_freshness: number; avg_consistency: number; avg_documents: number;
    open_errors: number; open_warns: number; resolved: number; remediations: number; top_checks: [string, number][]; delta: Record<string, number> } | null;
  sources: { rows: { source: string; rows: number; min_date: string | null; max_date: string | null; last_loaded: string | null; companies: number }[] | null; at: string | null; error: string | null } | null;
  errors: Record<string, string>;
  loader: { running: boolean; last: string };
  as_of: string;
};

const WATCHDOG_MIN = 60;   // the watchdog restarts a worker whose log is silent this long
const signed = (n: number | undefined) => (n === undefined ? "" : `${n > 0 ? "+" : ""}${n}`);
/** A failed section: small inline note, the rest of the page still renders. */
const SectionErr = ({ errs, keys }: { errs?: Record<string, string>; keys: string[] }) => {
  const m = keys.map((k) => errs?.[k]).filter(Boolean);
  return m.length ? <div className="text-[11.5px] text-bad">Could not refresh ({m.join("; ")}); showing the last good value if any.</div> : null;
};

/** Storage + overflow state: Supabase size against the pause limit and the 500 MB cap. */
function StorageBanner({ o, r }: { o: Ops | null; r: Rollout | null }) {
  const db = o?.db, paused = o?.control?.find((c) => c.key === "paused");
  if (!db && !r?.top100_primary) return <SectionErr errs={o?.errors} keys={["ops_storage"]} />;
  const mb = db ? db.bytes / 1048576 : 0, limit = Number(db?.limit_mb ?? 500), left = db ? db.cap_mb - mb : 0;
  const low = !!db && limit - mb < 20, isPaused = paused?.value === true || paused?.value === "true";
  return (
    <section className={`card space-y-2 p-3 ${isPaused ? "border-bad" : ""}`}>
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <b>Storage</b>
        {db && <span className="tabular-nums">{mb.toFixed(0)} MB of {db.cap_mb} MB cap · pause at {limit} MB · <span className={low ? "font-semibold text-bad" : ""}>{(limit - mb).toFixed(0)} MB until writers pause{low ? ", low" : ""}</span> · {left.toFixed(0)} MB to the cap</span>}
        {r?.top100_primary && <span className="text-[12px]">lake is primary for the top 100 since {dateTime(r.top100_primary.updated_at)}</span>}
      </div>
      {db && <Bar value={mb} max={db.cap_mb} tone={low || mb >= limit ? "var(--bad)" : "var(--accent)"} />}
      {isPaused && <div className="font-semibold text-bad">Writers paused: {paused?.reason || "no reason recorded"}</div>}
      <SectionErr errs={o?.errors} keys={["ops_storage"]} />
    </section>
  );
}

const MEASURES = [["screener", "screener.in statements"], ["yf", "Yahoo Finance"], ["ar", "Annual reports"], ["cc", "Concall transcripts"]] as const;

/** Rank-band rollout, workers, sources and data quality from the local lake (fetched by OpsPage every minute). */
function RolloutPanel({ r }: { r: Rollout }) {
  const bands = r.bands ?? [], runs = r.runs ?? [], dq = r.dq;
  const sum = (k: (typeof MEASURES)[number][0]) => bands.reduce((s, b) => s + b[k], 0);
  const total = bands.reduce((s, b) => s + b.total, 0);
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <section className="card space-y-2 p-3 lg:col-span-2">
        <div className="flex items-baseline justify-between"><b>Top {fmt(total, 0)} rollout</b><span className="text-[11.5px] text-muted">local lake · {dateTime(r.as_of)}</span></div>
        <SectionErr errs={r.errors} keys={["rollout_bands"]} />
        {MEASURES.map(([k, l]) => (
          <div key={k}><div className="flex justify-between"><span>{l}</span><span className="tabular-nums text-muted">{fmt(sum(k), 0)} / {fmt(total, 0)}</span></div><Bar value={sum(k)} max={total} /></div>
        ))}
        <table className="tbl mt-1"><thead><tr><th className="l">Ranks</th>{MEASURES.map(([k, l]) => <th key={k}>{l.split(" ")[0]}</th>)}</tr></thead>
          <tbody>{bands.map((b) => <tr key={b.from}><td className="l">{b.from}–{b.to}</td>{MEASURES.map(([k]) => <td key={k} className="num">{b[k]} <span className="text-muted">/ {b.total}</span></td>)}</tr>)}</tbody></table>
      </section>
      <section className="card space-y-2 p-3">
        <b>GitHub wave loading</b>
        <div className="text-[11.5px]"><StatusPill s={r.loader.running ? "running" : "idle"} /> <span className="text-muted">{r.loader.last}</span></div>
        <SectionErr errs={r.errors} keys={["rollout_runs"]} />
        {runs.length === 0 ? <div className="text-muted">Nothing loaded yet.</div> : (
          <table className="tbl"><tbody>{runs.map((x) => <tr key={x.run_id}><td className="l">#{x.run_id}</td><td className="num" title={`${fmt(x.rows_loaded, 0)} rows`}>{x.parts_loaded} parts</td><td className="l"><StatusPill s={x.complete ? "done" : "partial"} /></td><td className="l text-muted">{dateTime(x.last_at)}</td></tr>)}</tbody></table>
        )}
      </section>
      <section className="card p-3 lg:col-span-3">
        <div className="flex items-baseline gap-3"><b>Mac workers</b>{r.fleet_paused ? <span className="font-semibold text-bad">fleet paused (data/fleet/paused)</span> : <span className="text-[11.5px] text-muted">running</span>}</div>
        <table className="tbl mt-2"><tbody>{r.fleet.map((f) => (
          <tr key={f.name}><td className="l">{f.name}</td><td className="l"><StatusPill s={f.alive ? "ok" : "failed"} /></td>
            <td className={`num ${f.idle_min != null && f.idle_min > WATCHDOG_MIN ? "font-semibold text-bad" : "text-muted"}`} title={`watchdog restarts at ${WATCHDOG_MIN} min`}>{f.idle_min == null ? "no log" : `${Math.round(f.idle_min)} min silent`}</td>
            <td className="l text-muted">{dateTime(f.last_at)}</td><td className="l max-w-[640px] truncate" title={f.last}>{f.last}</td></tr>
        ))}</tbody></table>
      </section>
      <section className="card space-y-2 p-3 lg:col-span-2">
        <div className="flex items-baseline justify-between"><b>Public sources coverage</b><span className="text-[11.5px] text-muted">{r.sources?.at ? `computed ${dateTime(r.sources.at)}` : ""}</span></div>
        {r.sources?.error && <div className="text-[11.5px] text-bad">Last refresh failed: {r.sources.error}</div>}
        {!r.sources?.rows ? <div className="text-muted">Not computed yet (the view is slow; it is refreshed in the background).</div> : (
          <div className="tbl-wrap" style={{ maxHeight: 340 }}><table className="tbl"><thead><tr><th className="l">Source</th><th>Rows</th><th>Companies</th><th className="l">From</th><th className="l">To</th><th className="l">Last loaded</th></tr></thead>
            <tbody>{r.sources.rows.map((x) => <tr key={x.source}><td className="l">{x.source}</td><td className="num">{fmt(x.rows, 0)}</td><td className="num">{fmt(x.companies, 0)}</td><td className="l">{x.min_date ?? "–"}</td><td className="l">{x.max_date ?? "–"}</td><td className="l text-muted">{dateTime(x.last_loaded)}</td></tr>)}</tbody></table></div>
        )}
      </section>
      <section className="card space-y-2 p-3">
        <div className="flex items-baseline justify-between"><b>Data quality</b><span className="text-[11.5px] text-muted">{dq ? `${dq.as_of}${dq.prev_as_of ? ` vs ${dq.prev_as_of}` : ""}` : ""}</span></div>
        <SectionErr errs={r.errors} keys={["rollout_dq"]} />
        {!dq ? <div className="text-muted">No data-quality run yet.</div> : (
          <>
            <table className="tbl"><tbody>
              {([["Overall score", dq.avg_overall, "avg_overall"], ["Open errors", dq.open_errors, "open_errors"], ["Open warnings", dq.open_warns, "open_warns"], ["Resolved", dq.resolved, "resolved"], ["Companies", dq.companies, "companies"]] as const).map(([l, v, k]) => (
                <tr key={k}><td className="l">{l}</td><td className="num">{fmt(v, k === "avg_overall" ? 1 : 0)}</td><td className="num text-muted">{signed(dq.delta[k])}</td></tr>))}
              <tr><td className="l">Remediations</td><td className="num">{fmt(dq.remediations, 0)}</td><td /></tr>
            </tbody></table>
            <div className="text-[11.5px] text-muted">Top checks: {dq.top_checks.map(([c, n]) => `${c} ${n}`).join(", ")}</div>
          </>
        )}
      </section>
    </div>
  );
}

export default function OpsPage() {
  const [tick, setTick] = useState(0);
  useEffect(() => { const t = setInterval(() => setTick((n) => n + 1), 60_000); return () => clearInterval(t); }, []);
  const { data: o, error, loading } = useApi<Ops>("/api/ops");
  const { data: r, error: rerr } = useApi<Rollout>(`/api/rollout?t=${tick}`);
  return (
    <div className="space-y-4">
      <h1 className="text-[17px] font-semibold">Ops</h1>
      <StorageBanner o={o} r={r} />
      <CompletenessPanel />
      {rerr && !r ? <ErrorBox error={rerr} /> : r ? <RolloutPanel r={r} /> : <Skeleton h={160} />}
      {error ? <ErrorBox error={error} /> : loading && !o ? <div className="space-y-3"><Skeleton h={80} /><TableSkeleton /></div> : o ? <OpsBody o={o} /> : null}
    </div>
  );
}

function OpsBody({ o }: { o: Ops }) {
  const mb = o.db ? o.db.bytes / 1048576 : 0;   // MiB, the unit of storage_limit_mb
  const c = o.completeness ?? { ar_expected: 0, ar_found: 0, cc_expected: 0, cc_found: 0, ppt_expected: 0, ppt_found: 0, companies: 0 };
  const runs = o.runs ?? [], jobs = o.jobs ?? [], documents = o.documents ?? [], control = o.control ?? [];
  const dur = (r: NonNullable<Ops["runs"]>[0]) => (r.finished_at ? `${Math.round((+new Date(r.finished_at) - +new Date(r.started_at)) / 60000)}m` : "running");
  const kinds = [...new Set(jobs.map((j) => j.kind))], sts = [...new Set(jobs.map((j) => j.status))];
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-3">
        <section className="card space-y-2 p-3">
          <b>Supabase size</b>
          <SectionErr errs={o.errors} keys={["ops_storage"]} />
          {o.db && <><div className="text-[20px] font-semibold tabular-nums">{mb.toFixed(0)} MB <span className="text-[12px] font-normal text-muted">of {o.db.cap_mb} MB cap (pause at {String(o.db.limit_mb)} MB)</span></div>
          <Bar value={mb} max={o.db.cap_mb} tone={mb > Number(o.db.limit_mb) ? "var(--bad)" : "var(--accent)"} /></>}
          <table className="tbl"><tbody>{(o.db?.tables ?? []).slice(0, 6).map((t) => <tr key={t.name}><td className="l">{t.name}</td><td className="num">{bytes(t.bytes)}</td><td className="num text-muted">{fmt(t.est_rows, 0)} rows</td></tr>)}</tbody></table>
        </section>
        <section className="card space-y-2 p-3">
          <b>Document completeness (top 100)</b>
          <SectionErr errs={o.errors} keys={["ops_completeness"]} />
          {([["Annual reports", c.ar_found, c.ar_expected], ["Concall transcripts", c.cc_found, c.cc_expected], ["Concall PPTs", c.ppt_found, c.ppt_expected]] as const).map(([l, f, e]) => (
            <div key={l}><div className="flex justify-between"><span>{l}</span><span className="tabular-nums text-muted">{fmt(f, 0)} / {fmt(e, 0)}</span></div><Bar value={f} max={e} /></div>
          ))}
          <div className="text-[11.5px] text-muted">Worst gaps: {(o.completeness_gaps ?? []).slice(0, 6).map((g) => `${g.symbol} (${g.ar_gap}+${g.cc_gap})`).join(", ")}</div>
        </section>
        <section className="card space-y-2 p-3">
          <b>Local library index</b>
          {o.library.built ? <div className="tabular-nums">{fmt(o.library.docs, 0)} documents · {fmt(o.library.chunks, 0)} passages · {o.library.size_mb} MB</div> : <div className="text-muted">Not built. Run <code>.venv/bin/python -m library.index</code>.</div>}
          <b className="block pt-2">Documents by status</b>
          <SectionErr errs={o.errors} keys={["ops_documents"]} />
          <table className="tbl"><tbody>{documents.map((d) => <tr key={d.doc_type + d.status}><td className="l">{d.doc_type}</td><td className="l"><StatusPill s={d.status} /></td><td className="num">{d.n}</td></tr>)}</tbody></table>
        </section>
      </div>
      <section className="card p-3">
        <b>Job queue</b>
        <SectionErr errs={o.errors} keys={["ops_queue"]} />
        {jobs.length === 0 ? <div className="mt-2 text-muted">No jobs queued (the queue is empty).</div> : (
          <table className="tbl mt-2"><thead><tr><th className="l">Kind</th>{sts.map((s) => <th key={s}>{s}</th>)}</tr></thead>
            <tbody>{kinds.map((k) => <tr key={k}><td className="l">{k}</td>{sts.map((s) => <td key={s} className="num">{jobs.find((j) => j.kind === k && j.status === s)?.n ?? 0}</td>)}</tr>)}</tbody></table>
        )}
      </section>
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card p-3"><b>Failures by error class</b>
          {(o.job_failures?.length ?? 0) + (o.task_failures?.length ?? 0) === 0 ? <div className="mt-2 text-muted">No failures recorded.</div> : (
            <table className="tbl mt-2"><tbody>
              {(o.job_failures ?? []).map((f, i) => <tr key={"j" + i}><td className="l">job:{f.kind}</td><td className="l">{f.error_class}</td><td className="num">{f.n}</td></tr>)}
              {(o.task_failures ?? []).map((f, i) => <tr key={"t" + i}><td className="l">{f.task}</td><td className="l max-w-[420px] truncate">{f.error}</td><td className="num">{f.n}</td></tr>)}
            </tbody></table>)}
        </section>
        <section className="card p-3"><b>Control switches</b>
          <table className="tbl mt-2"><tbody>{control.map((c) => <tr key={c.key}><td className="l">{c.key}</td><td className="l">{String(c.value)}</td><td className="l text-muted">{c.reason}</td></tr>)}</tbody></table>
        </section>
      </div>
      <section className="card p-3"><b>Recent pipeline runs</b>
        <SectionErr errs={o.errors} keys={["ops_runs"]} />
        {runs.length === 0 ? <Empty title="No runs yet" /> : (
          <div className="tbl-wrap mt-2" style={{ maxHeight: 360 }}><table className="tbl"><thead><tr><th className="l">Run</th><th className="l">Command</th><th className="l">Status</th><th className="l">Started</th><th>Took</th><th className="l">Host</th><th className="l">Stats</th></tr></thead>
            <tbody>{runs.map((r) => <tr key={r.run_id}><td className="l">#{r.run_id}</td><td className="l">{r.command}</td><td className="l"><StatusPill s={r.status} /></td><td className="l">{dateTime(r.started_at)}</td><td className="num">{dur(r)}</td><td className="l">{r.host}</td><td className="l text-muted">{JSON.stringify(r.stats)}</td></tr>)}</tbody></table></div>
        )}
      </section>
    </div>
  );
}
