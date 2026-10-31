"use client";
import Link from "next/link";
import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { getJSON, qs, useApi } from "@/lib/api";
import { date, DOC_LABEL, fmt } from "@/lib/format";
import { Empty, ErrorBox, Skeleton, StatusPill } from "@/components/ui";
import { Highlight } from "@/components/Mark";

type Doc = { meta: { symbol: string; name: string; doc_type: string; period_label: string | null; fiscal_year: number | null; url: string; fetched_url: string | null; status: string; source: string | null;
  fetch_source: string | null; page_count: number | null; char_count: number | null; doc_date: string | null; extractor: string | null; citation: string }; total_pages: number | null; pages: { page: number; text: string }[]; available: boolean };
type Find = { terms: string[]; total: number; pages: { page: number; count: number; snippet: string }[] };

function Inner() {
  const sp = useSearchParams(), router = useRouter();
  const key = sp.get("key") || "";
  const page = Math.max(1, parseInt(sp.get("page") || "1", 10) || 1);
  const q = sp.get("q") || "";
  const [find, setFind] = useState(q);
  const [jump, setJump] = useState(String(page));
  const [res, setRes] = useState<Find | null>(null);
  const [copied, setCopied] = useState(false);
  const [findErr, setFindErr] = useState<string | null>(null);
  const { data, error, loading } = useApi<Doc>(key ? `/api/documents/${key}?page=${page}&limit=1` : null);
  const total = data?.total_pages ?? 0;
  const go = useCallback((p: number) => router.replace(`/doc/${qs({ key, page: Math.min(Math.max(1, p), total || p), q })}`), [router, key, q, total]);
  useEffect(() => setJump(String(page)), [page]);
  useEffect(() => {
    const on = (e: KeyboardEvent) => { const t = e.target as HTMLElement; if (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable || e.metaKey || e.ctrlKey || e.altKey) return; if (e.key === "j" || e.key === "ArrowRight") go(page + 1); if (e.key === "k" || e.key === "ArrowLeft") go(page - 1); };
    window.addEventListener("keydown", on); return () => window.removeEventListener("keydown", on);
  }, [go, page]);
  useEffect(() => {
    setRes(null); setFindErr(null);
    if (!q.trim() || !key) return;
    let live = true; getJSON<Find>(`/api/documents/${key}/find${qs({ q })}`).then((r) => live && setRes(r)).catch((e) => live && setFindErr(String(e.message ?? e))); return () => { live = false; };
  }, [q, key]);
  if (!key) return <Empty title="No document" hint={<Link className="underline" href="/library/">Open the library</Link>} />;
  if (error) return <ErrorBox error={error} />;
  if (!data) return loading ? <Skeleton h={500} /> : null;
  const m = data.meta, terms = res?.terms ?? [];
  const cite = `${m.citation}
${m.url}`;
  return (
    <div className="grid gap-4 lg:grid-cols-[300px_1fr]">
      <aside className="space-y-3 lg:sticky lg:top-14 lg:self-start">
        <div className="card space-y-1 p-3">
          <Link className="font-semibold text-brand hover:underline" href={`/company/?s=${m.symbol}&tab=documents`}>{m.symbol}</Link> <span className="text-muted">{m.name}</span>
          <div>{DOC_LABEL[m.doc_type]} · {m.period_label}</div>
          <div className="flex flex-wrap items-center gap-1"><StatusPill s={m.status} /><span className="pill">{fmt(m.page_count, 0)} pages</span><span className="pill">{m.source ?? "?"}{m.fetch_source ? ` / ${m.fetch_source}` : ""}</span></div>
          {m.doc_date && <div className="text-muted">Dated {date(m.doc_date)}</div>}
          <div className="flex gap-2 pt-1">
            <a className="btn" href={m.fetched_url || m.url} target="_blank" rel="noreferrer">Original PDF</a>
            <button className="btn" onClick={() => { navigator.clipboard?.writeText(cite); setCopied(true); setTimeout(() => setCopied(false), 1500); }}>{copied ? "Copied" : "Cite"}</button>
          </div>
        </div>
        <div className="card space-y-2 p-3">
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); const p = parseInt(jump, 10); if (p) go(p); }}>
            <button type="button" className="btn" onClick={() => go(page - 1)} disabled={page <= 1}>‹</button>
            <input className="input w-16 text-center" value={jump} onChange={(e) => setJump(e.target.value)} aria-label="Page" />
            <span className="self-center text-muted">/ {total}</span>
            <button type="button" className="btn" onClick={() => go(page + 1)} disabled={page >= total}>›</button>
          </form>
          <input data-search className="input w-full" placeholder="Find in document   ( / )" value={find} onChange={(e) => setFind(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") router.replace(`/doc/${qs({ key, page, q: find })}`); }} />
          {findErr && <div className="text-bad">Find failed: {findErr}</div>}
          {res && <div className="text-muted">{res.total} matches on {res.pages.length} pages</div>}
          <div className="max-h-72 space-y-1 overflow-auto">
            {res?.pages.map((r) => <button key={r.page} className={`block w-full rounded-md p-1.5 text-left hover:bg-surface-2 ${r.page === page ? "bg-surface-2" : ""}`} onClick={() => go(r.page)}><b>p.{r.page}</b> <span className="text-muted">×{r.count}</span><div className="line-clamp-2 text-[11.5px] text-muted">{r.snippet}</div></button>)}
          </div>
        </div>
      </aside>
      <article className="card p-6">
        {!data.available ? <Empty title="Text file not available locally yet" hint="The document is cataloged but its extracted text has not been downloaded into data/documents." /> : (
          data.pages.length === 0 ? <Empty title="Page out of range" /> :
          data.pages.map((p) => <div key={p.page}><div className="mb-3 text-[11px] uppercase tracking-wide text-muted">Page {p.page} of {total}</div><div className="reader-text"><Highlight text={p.text || "(blank page)"} terms={terms} /></div></div>)
        )}
      </article>
    </div>
  );
}
export default function Page() { return <Suspense><Inner /></Suspense>; }
