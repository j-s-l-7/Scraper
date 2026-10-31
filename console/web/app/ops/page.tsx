"use client";
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

export default function OpsPage() {
  const { data: o, error, loading } = useApi<Ops>("/api/ops");
  if (error) return <ErrorBox error={error} />;
  if (loading && !o) return <div className="space-y-3"><Skeleton h={80} /><TableSkeleton /></div>;
  if (!o) return null;
  const mb = o.db.bytes / 1e6, c = o.completeness;
  const dur = (r: Ops["runs"][0]) => (r.finished_at ? `${Math.round((+new Date(r.finished_at) - +new Date(r.started_at)) / 60000)}m` : "running");
  const kinds = [...new Set(o.jobs.map((j) => j.kind))], sts = [...new Set(o.jobs.map((j) => j.status))];
  return (
    <div className="space-y-4">
      <h1 className="text-[17px] font-semibold">Ops</h1>
      <div className="grid gap-4 lg:grid-cols-3">
        <section className="card space-y-2 p-3">
          <b>Database size</b>
          <div className="text-[20px] font-semibold tabular-nums">{mb.toFixed(0)} MB <span className="text-[12px] font-normal text-muted">of {o.db.cap_mb} MB cap (pause at {String(o.db.limit_mb)} MB)</span></div>
          <Bar value={mb} max={o.db.cap_mb} tone={mb > o.db.limit_mb ? "var(--bad)" : "var(--accent)"} />
          <table className="tbl"><tbody>{o.db.tables.slice(0, 6).map((t) => <tr key={t.name}><td className="l">{t.name}</td><td className="num">{bytes(t.bytes)}</td><td className="num text-muted">{fmt(t.est_rows, 0)} rows</td></tr>)}</tbody></table>
        </section>
        <section className="card space-y-2 p-3">
          <b>Document completeness</b>
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
