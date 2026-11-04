"use client";
import { useEffect, useState } from "react";

const mem = new Map<string, { t: number; v: unknown }>();
const TTL = 60_000;

export async function getJSON<T>(url: string, signal?: AbortSignal): Promise<T> {
  const hit = mem.get(url);
  if (hit && Date.now() - hit.t < TTL) return hit.v as T;
  const r = await fetch(url, { signal });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch {}
    throw new Error(`${r.status} ${detail}`);
  }
  const v = (await r.json()) as T;
  mem.set(url, { t: Date.now(), v });
  return v;
}

export type State<T> = { data: T | null; error: string | null; loading: boolean };

/** Client-side fetch of /api/*; `url` null skips. Keeps the previous data while a new url loads. */
export function useApi<T>(url: string | null): State<T> {
  const [st, setSt] = useState<State<T>>({ data: null, error: null, loading: !!url });
  useEffect(() => {
    if (!url) { setSt({ data: null, error: null, loading: false }); return; }
    const ac = new AbortController();
    setSt((s) => ({ data: s.data, error: null, loading: true }));
    getJSON<T>(url, ac.signal)
      .then((data) => setSt({ data, error: null, loading: false }))
      .catch((e) => { if (e.name !== "AbortError") setSt((s) => ({ data: s.data, error: String(e.message ?? e), loading: false })); });
    return () => ac.abort();
  }, [url]);
  return st;
}

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "" && v !== false) u.set(k, String(v));
  const s = u.toString();
  return s ? `?${s}` : "";
}

// ---- response types (only what the UI reads)
export type Security = {
  rank: number; company_id: number; symbol: string; name: string; sector: string | null; industry: string | null;
  snapshot_as_of: string | null; market_cap_cr: number | null; price: number | null; pe: number | null; roe_pct: number | null;
  roce_pct: number | null; high_52w: number | null; low_52w: number | null; dividend_yield_pct: number | null; ret_1y: number | null;
  has_screener: boolean; has_yf: boolean; ar_n: number; cc_n: number; doc_gaps: number;
  complete_pct: number | null; covered_pct: number | null; open_gaps: number | null;
};
export type FinRow = { metric_id: number; name: string; unit: string; values: (number | null)[]; ttm: number | null; children: FinRow[] };
export type Financials = {
  symbol: string; basis: string; source: string; section: string; period: string; available_periods: string[]; bases_available: string | null;
  sections: string[]; columns: { period_end: string; label: string }[]; ttm: { period_end: string; label: string } | null; rows: FinRow[]; units_note: string;
};
export type DocItem = {
  key: string; symbol: string; doc_type: string; url: string; period_label: string | null; fiscal_year: number | null; doc_date: string | null;
  source: string | null; status: string; page_count: number | null; char_count: number | null; fetch_source: string | null; fetched_url: string | null;
  error: string | null; has_file: boolean; in_library: boolean; name?: string; extractor?: string | null; extracted_at?: string | null;
};
export type Hit = {
  chunk_id: number; key: string; symbol: string; name: string; doc_type: string; fiscal_year: number | null; period_label: string | null;
  page_from: number; page_to: number; heading: string | null; snippet: string; score: number; strict: boolean; citation: string; url: string;
};
