"use client";
import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { getJSON, Hit, qs } from "@/lib/api";
import { DOC_LABEL, fmt } from "@/lib/format";
import { Empty, ErrorBox, Skeleton } from "@/components/ui";
import { Marked } from "@/components/Mark";

type Res = { hits: Hit[]; total_strict: number; terms: string[]; facets: { companies: { symbol: string; name: string; n: number }[]; doc_types: { doc_type: string; n: number }[]; fiscal_years: { fiscal_year: number; n: number }[] } | null;
  stats: { built: boolean; docs: number; chunks: number; size_mb: number; fy_min?: number; fy_max?: number } };

function Inner() {
  const sp = useSearchParams(), router = useRouter();
  const q = sp.get("q") || "", symbols = sp.get("symbols") || "", types = sp.get("types") || "", fyf = sp.get("fy_from") || "", fyt = sp.get("fy_to") || "";
  const [input, setInput] = useState(q);
  const [res, setRes] = useState<Res | null>(null);
  const [facets, setFacets] = useState<Res["facets"]>(null);
  const [hits, setHits] = useState<Hit[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stats, setStats] = useState<Res["stats"] | null>(null);

  const push = (o: Record<string, string>) => router.replace(`/library/${qs({ q, symbols, types, fy_from: fyf, fy_to: fyt, ...o })}`);
  useEffect(() => { getJSON<Res["stats"]>("/api/library/stats").then(setStats).catch(() => {}); }, []);
  useEffect(() => { const id = setTimeout(() => { if (input !== q) push({ q: input }); }, 300); return () => clearTimeout(id); /* eslint-disable-next-line */ }, [input]);
  useEffect(() => {
    if (!q.trim()) { setRes(null); setHits([]); return; }
    let live = true; setBusy(true); setErr(null);
    getJSON<Res>(`/api/library/search${qs({ q, symbols, types, fy_from: fyf, fy_to: fyt, limit: 15 })}`)
      .then((r) => { if (!live) return; setRes(r); setHits(r.hits); if (r.facets) setFacets(r.facets); })
      .catch((e) => live && setErr(String(e.message))).finally(() => live && setBusy(false));
    return () => { live = false; };
  }, [q, symbols, types, fyf, fyt]);
  const more = async () => {
    setBusy(true);
    try { const r = await getJSON<Res>(`/api/library/search${qs({ q, symbols, types, fy_from: fyf, fy_to: fyt, limit: 15, offset: hits.length, facets: false })}`); setHits((h) => [...h, ...r.hits]); } catch (e) { setErr(String((e as Error).message)); } finally { setBusy(false); }
  };
  const toggle = (cur: string, v: string) => { const s = new Set(cur ? cur.split(",") : []); s.has(v) ? s.delete(v) : s.add(v); return [...s].join(","); };
  const selS = symbols ? symbols.split(",") : [], selT = types ? types.split(",") : [];
  const companies = facets ? [...facets.companies, ...selS.filter((s) => !facets.companies.some((c) => c.symbol === s)).map((s) => ({ symbol: s, name: "", n: 0 }))] : selS.map((s) => ({ symbol: s, name: "", n: 0 }));
  const fyMin = stats?.fy_min ?? 2015, fyMax = stats?.fy_max ?? 2026, fyOpts = Array.from({ length: fyMax - fyMin + 1 }, (_, i) => fyMin + i);

  return (
    <div className="grid gap-4 lg:grid-cols-[250px_1fr]">
      <aside className="space-y-4">
        <div className="card space-y-2 p-3">
          <b>Companies</b>
          <div className="max-h-56 space-y-1 overflow-auto">{companies.length === 0 && <div className="text-muted">Search to see companies</div>}
            {companies.map((c) => <label key={c.symbol} className="flex cursor-pointer items-center gap-2"><input type="checkbox" checked={selS.includes(c.symbol)} onChange={() => push({ symbols: toggle(symbols, c.symbol) })} /><span className="flex-1 truncate">{c.symbol}</span><span className="text-muted tabular-nums">{c.n || ""}</span></label>)}</div>
        </div>
        <div className="card space-y-1 p-3"><b>Document type</b>
          {["annual_report", "concall_transcript", "concall_ppt"].map((t) => <label key={t} className="flex cursor-pointer items-center gap-2"><input type="checkbox" checked={selT.includes(t)} onChange={() => push({ types: toggle(types, t) })} /><span className="flex-1">{DOC_LABEL[t]}</span><span className="text-muted tabular-nums">{facets?.doc_types.find((d) => d.doc_type === t)?.n ?? ""}</span></label>)}
        </div>
        <div className="card space-y-2 p-3"><b>Fiscal year</b>
          <div className="flex items-center gap-2">
            <select className="input flex-1" value={fyf} onChange={(e) => push({ fy_from: e.target.value })}><option value="">from</option>{fyOpts.map((y) => <option key={y}>{y}</option>)}</select>
            <select className="input flex-1" value={fyt} onChange={(e) => push({ fy_to: e.target.value })}><option value="">to</option>{fyOpts.map((y) => <option key={y}>{y}</option>)}</select>
          </div>
        </div>
        {(symbols || types || fyf || fyt) && <button className="btn" onClick={() => push({ symbols: "", types: "", fy_from: "", fy_to: "" })}>Clear filters</button>}
      </aside>
      <section className="space-y-3">
        <div className="flex items-center gap-2">
          <input data-search autoFocus className="input flex-1 text-[14px]" placeholder={'Search annual reports and concalls, e.g. capital allocation, "free cash flow"   ( / )'} value={input} onChange={(e) => setInput(e.target.value)} />
        </div>
        <div className="text-muted tabular-nums">{stats ? (stats.built ? `${fmt(stats.docs, 0)} documents · ${fmt(stats.chunks, 0)} passages indexed · ${stats.size_mb} MB` : "Index not built") : ""}{res && q ? ` · ${fmt(res.total_strict, 0)} passages match all terms` : ""}</div>
        {err && <ErrorBox error={err} />}
        {!q.trim() && (stats?.built === false ? <Empty title="The library index is not built" hint={<>Run <code>.venv/bin/python -m library.index</code> and refresh.</>} /> : <Empty title="Search the library" hint="Full-text search across every indexed annual report and concall transcript. Use quotes for phrases. Press / to focus." />)}
        {busy && !hits.length && q && [0, 1, 2, 3].map((i) => <Skeleton key={i} h={90} />)}
        {q && !busy && res && hits.length === 0 && <Empty title="No passages found" hint="Try fewer words or clear the filters." />}
        {hits.map((h) => (
          <article key={h.chunk_id} className="card p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Link className="font-medium text-brand hover:underline" href={`/doc/?key=${h.key}&page=${h.page_from}&q=${encodeURIComponent(q)}`}>{h.citation}</Link>
              <span className={`pill ${h.strict ? "ok" : ""}`}>{h.strict ? "all terms" : "related"}</span>
              <span className="ml-auto text-[11px] text-muted tabular-nums">score {h.score.toFixed(2)}</span>
            </div>
            {h.heading && <div className="mt-0.5 text-[12px] text-muted">{h.heading}</div>}
            <p className="mt-1 text-[13.5px] leading-relaxed"><Marked text={h.snippet} /></p>
          </article>
        ))}
        {res && hits.length > 0 && hits.length % 15 === 0 && <button className="btn" onClick={more} disabled={busy}>{busy ? "Loading…" : "Load more"}</button>}
      </section>
    </div>
  );
}
export default function Page() { return <Suspense><Inner /></Suspense>; }
