import React from "react";
/** Renders text containing <mark>..</mark> markers (from the search API) without innerHTML. */
export function Marked({ text }: { text: string }) {
  const parts = text.split(/(<mark>.*?<\/mark>)/g);
  return <>{parts.map((p, i) => p.startsWith("<mark>") ? <mark key={i}>{p.slice(6, -7)}</mark> : <React.Fragment key={i}>{p}</React.Fragment>)}</>;
}
const SUFFIXES = ["ements", "ement", "ations", "ation", "ings", "ing", "ies", "ied", "ers", "er", "ed", "es", "s", "e", "y"];
export function stemOf(w: string) { for (const x of SUFFIXES) if (w.endsWith(x) && w.length - x.length >= 3) return w.slice(0, w.length - x.length); return w; }
/** Highlights query terms (stem-prefix match, like the search index) in plain text. */
export function Highlight({ text, terms }: { text: string; terms: string[] }) {
  const ts = terms.map((t) => t.toLowerCase().replace(/[^a-z0-9]/g, "")).filter((t) => t.length >= 2).map(stemOf);
  if (!ts.length) return <>{text}</>;
  const rx = new RegExp(`\\b(?:${ts.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})\\w*`, "gi");
  const out: React.ReactNode[] = []; let last = 0, m: RegExpExecArray | null, k = 0;
  while ((m = rx.exec(text))) { if (m.index > last) out.push(text.slice(last, m.index)); out.push(<mark key={k++}>{m[0]}</mark>); last = m.index + m[0].length; if (m[0].length === 0) rx.lastIndex++; }
  out.push(text.slice(last));
  return <>{out}</>;
}
