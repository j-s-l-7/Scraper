"use client";
import { useEffect, useState } from "react";
import { useApi } from "@/lib/api";
import { bytes, dateTime, fmt } from "@/lib/format";
import { Bar, Empty, ErrorBox, Skeleton, StatusPill, TableSkeleton } from "@/components/ui";

type Ops = {
  runs: { run_id: number; command: string; status: string; stats: Record<string, unknown>; host: string | null; started_at: string; finished_at: string | null }[];
  jobs: { kind: string; status: string; n: number }[];
  job_failures: { kind: string; error_class: string; n: number }[];
  task_failures: { task: string; error: string; n: number }[];
  db: { bytes: number; limit_mb: number; cap_mb: number; tables: { name: string; bytes: number; est_rows: number }[] };
  documents: { doc_type: string; status: string; n: number }[];
  completeness: { ar_expected: number; ar_found: number; cc_expected: number; cc_found: number; ppt_expected: number; ppt_found: number; companies: number };
  completeness_gaps: { symbol: string; ar_gap: number; cc_gap: number }[];
  control: { key: string; value: unknown; reason: string | null }[];
  library: { built: boolean; docs: number; chunks: number; size_mb: number };
};

type Rollout = {
  bands: { from: number; to: number; total: number; screener: number; yf: number; ar: number; cc: number }[];
  runs: { run_id: number; parts_loaded: number; rows_loaded: number; complete: boolean; last_at: string | null }[];
  fleet: { name: string; alive: boolean; last: string; last_at: string | null }[];
  loader: { running: boolean; last: string };
  as_of: string;
};

const MEASURES = [["screener", "screener.in statements"], ["yf", "Yahoo Finance"], ["ar", "Annual reports"], ["cc", "Concall transcripts"]] as const;

/** Top-1000 rollout from the local lake; refetches every minute. */
function RolloutPanel() {
  const [tick, setTick] = useState(0);
  useEffect(() => { const t = setInterval(() => setTick((n) => n + 1), 60_000); return () => clearInterval(t); }, []);
  const { data: r, error } = useApi<Rollout>(`/api/rollout?t=${tick}`);
  if (error && !r) return <ErrorBox error={error} />;
  if (!r) return <Skeleton h={160} />;
  const sum = (k: (typeof MEASURES)[number][0]) => r.bands.reduce((s, b) => s + b[k], 0);
  const total = r.bands.reduce((s, b) => s + b.total, 0);
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <section className="card space-y-2 p-3 lg:col-span-2">
        <div className="flex items-baseline justify-between"><b>Top {fmt(total, 0)} rollout</b><span className="text-[11.5px] text-muted">local lake · {dateTime(r.as_of)}</span></div>
        {MEASURES.map(([k, l]) => (
          <div key={k}><div className="flex justify-between"><span>{l}</span><span className="tabular-nums text-muted">{fmt(sum(k), 0)} / {fmt(total, 0)}</span></div><Bar value={sum(k)} max={total} /></div>
        ))}
        <table className="tbl mt-1"><thead><tr><th className="l">Ranks</th>{MEASURES.map(([k, l]) => <th key={k}>{l.split(" ")[0]}</th>)}</tr></thead>
          <tbody>{r.bands.map((b) => <tr key={b.from}><td className="l">{b.from}–{b.to}</td>{MEASURES.map(([k]) => <td key={k} className="num">{b[k]} <span className="text-muted">/ {b.total}</span></td>)}</tr>)}</tbody></table>
      </section>
      <section className="card space-y-2 p-3">
        <b>GitHub wave loading</b>
        <div className="text-[11.5px]"><StatusPill s={r.loader.running ? "running" : "idle"} /> <span className="text-muted">{r.loader.last}</span></div>
        {r.runs.length === 0 ? <div className="text-muted">Nothing loaded yet.</div> : (
          <table className="tbl"><tbody>{r.runs.map((x) => <tr key={x.run_id}><td className="l">#{x.run_id}</td><td className="num" title={`${fmt(x.rows_loaded, 0)} rows`}>{x.parts_loaded} parts</td><td className="l"><StatusPill s={x.complete ? "done" : "partial"} /></td><td className="l text-muted">{dateTime(x.last_at)}</td></tr>)}</tbody></table>
        )}
      </section>
      <section className="card p-3 lg:col-span-3">
        <b>Mac workers</b>
        <table className="tbl mt-2"><tbody>{r.fleet.map((f) => (
          <tr key={f.name}><td className="l">{f.name}</td><td className="l"><StatusPill s={f.alive ? "ok" : "failed"} /></td>
            <td className="l text-muted">{dateTime(f.last_at)}</td><td className="l max-w-[640px] truncate" title={f.last}>{f.last}</td></tr>
        ))}</tbody></table>
      </section>
    </div>
  );
}

export default function OpsPage() {
  const { data: o, error, loading } = useApi<Ops>("/api/ops");
  return (
    <div className="space-y-4">
      <h1 className="text-[17px] font-semibold">Ops</h1>
      <RolloutPanel />
      {error ? <ErrorBox error={error} /> : loading && !o ? <div className="space-y-3"><Skeleton h={80} /><TableSkeleton /></div> : o ? <OpsBody o={o} /> : null}
    </div>
  );
}

function OpsBody({ o }: { o: Ops }) {
  const mb = o.db.bytes / 1048576, c = o.completeness;   // MiB, the unit of storage_limit_mb
  const dur = (r: Ops["runs"][0]) => (r.finished_at ? `${Math.round((+new Date(r.finished_at) - +new Date(r.started_at)) / 60000)}m` : "running");
  const kinds = [...new Set(o.jobs.map((j) => j.kind))], sts = [...new Set(o.jobs.map((j) => j.status))];
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-3">
        <section className="card space-y-2 p-3">
          <b>Supabase size</b>
          <div className="text-[20px] font-semibold tabular-nums">{mb.toFixed(0)} MB <span className="text-[12px] font-normal text-muted">of {o.db.cap_mb} MB cap (pause at {String(o.db.limit_mb)} MB)</span></div>
          <Bar value={mb} max={o.db.cap_mb} tone={mb > o.db.limit_mb ? "var(--bad)" : "var(--accent)"} />
          <table className="tbl"><tbody>{o.db.tables.slice(0, 6).map((t) => <tr key={t.name}><td className="l">{t.name}</td><td className="num">{bytes(t.bytes)}</td><td className="num text-muted">{fmt(t.est_rows, 0)} rows</td></tr>)}</tbody></table>
        </section>
        <section className="card space-y-2 p-3">
          <b>Document completeness (top 100)</b>
          {([["Annual reports", c.ar_found, c.ar_expected], ["Concall transcripts", c.cc_found, c.cc_expected], ["Concall PPTs", c.ppt_found, c.ppt_expected]] as const).map(([l, f, e]) => (
            <div key={l}><div className="flex justify-between"><span>{l}</span><span className="tabular-nums text-muted">{fmt(f, 0)} / {fmt(e, 0)}</span></div><Bar value={f} max={e} /></div>
          ))}
          <div className="text-[11.5px] text-muted">Worst gaps: {o.completeness_gaps.slice(0, 6).map((g) => `${g.symbol} (${g.ar_gap}+${g.cc_gap})`).join(", ")}</div>
        </section>
        <section className="card space-y-2 p-3">
          <b>Local library index</b>
          {o.library.built ? <div className="tabular-nums">{fmt(o.library.docs, 0)} documents · {fmt(o.library.chunks, 0)} passages · {o.library.size_mb} MB</div> : <div className="text-muted">Not built. Run <code>.venv/bin/python -m library.index</code>.</div>}
          <b className="block pt-2">Documents by status</b>
          <table className="tbl"><tbody>{o.documents.map((d) => <tr key={d.doc_type + d.status}><td className="l">{d.doc_type}</td><td className="l"><StatusPill s={d.status} /></td><td className="num">{d.n}</td></tr>)}</tbody></table>
        </section>
      </div>
      <section className="card p-3">
        <b>Job queue</b>
        {o.jobs.length === 0 ? <div className="mt-2 text-muted">No jobs queued (the queue is empty).</div> : (
          <table className="tbl mt-2"><thead><tr><th className="l">Kind</th>{sts.map((s) => <th key={s}>{s}</th>)}</tr></thead>
            <tbody>{kinds.map((k) => <tr key={k}><td className="l">{k}</td>{sts.map((s) => <td key={s} className="num">{o.jobs.find((j) => j.kind === k && j.status === s)?.n ?? 0}</td>)}</tr>)}</tbody></table>
        )}
      </section>
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card p-3"><b>Failures by error class</b>
          {o.job_failures.length + o.task_failures.length === 0 ? <div className="mt-2 text-muted">No failures recorded.</div> : (
            <table className="tbl mt-2"><tbody>
              {o.job_failures.map((f, i) => <tr key={"j" + i}><td className="l">job:{f.kind}</td><td className="l">{f.error_class}</td><td className="num">{f.n}</td></tr>)}
              {o.task_failures.map((f, i) => <tr key={"t" + i}><td className="l">{f.task}</td><td className="l max-w-[420px] truncate">{f.error}</td><td className="num">{f.n}</td></tr>)}
            </tbody></table>)}
        </section>
        <section className="card p-3"><b>Control switches</b>
          <table className="tbl mt-2"><tbody>{o.control.map((c) => <tr key={c.key}><td className="l">{c.key}</td><td className="l">{String(c.value)}</td><td className="l text-muted">{c.reason}</td></tr>)}</tbody></table>
        </section>
      </div>
      <section className="card p-3"><b>Recent pipeline runs</b>
        {o.runs.length === 0 ? <Empty title="No runs yet" /> : (
          <div className="tbl-wrap mt-2" style={{ maxHeight: 360 }}><table className="tbl"><thead><tr><th className="l">Run</th><th className="l">Command</th><th className="l">Status</th><th className="l">Started</th><th>Took</th><th className="l">Host</th><th className="l">Stats</th></tr></thead>
            <tbody>{o.runs.map((r) => <tr key={r.run_id}><td className="l">#{r.run_id}</td><td className="l">{r.command}</td><td className="l"><StatusPill s={r.status} /></td><td className="l">{dateTime(r.started_at)}</td><td className="num">{dur(r)}</td><td className="l">{r.host}</td><td className="l text-muted">{JSON.stringify(r.stats)}</td></tr>)}</tbody></table></div>
        )}
      </section>
    </div>
  );
}
