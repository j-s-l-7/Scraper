"use client";
import React from "react";

export function Skeleton({ h = 16, w = "100%", className = "" }: { h?: number; w?: number | string; className?: string }) {
  return <div className={`sk ${className}`} style={{ height: h, width: w }} />;
}
export function TableSkeleton({ rows = 10 }: { rows?: number }) {
  return (
    <div className="card space-y-2 p-4">
      {Array.from({ length: rows }).map((_, i) => <Skeleton key={i} h={18} w={`${95 - (i % 4) * 6}%`} />)}
    </div>
  );
}
export function Empty({ title, hint }: { title: string; hint?: React.ReactNode }) {
  return (
    <div className="card grid place-items-center gap-1 px-6 py-10 text-center">
      <div className="font-medium">{title}</div>
      {hint && <div className="max-w-md text-muted">{hint}</div>}
    </div>
  );
}
export function ErrorBox({ error }: { error: string }) {
  return <div className="card border-bad p-4 text-bad">Could not load: {error}</div>;
}
export function Seg<T extends string>({ value, options, onChange }: { value: T; options: { v: T; label: string; disabled?: boolean }[]; onChange: (v: T) => void }) {
  return (
    <div className="inline-flex gap-1">
      {options.map((o) => (
        <button key={o.v} className="btn" aria-pressed={value === o.v} disabled={o.disabled} onClick={() => onChange(o.v)} style={o.disabled ? { opacity: 0.4 } : undefined}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
export function Stat({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: "bad" | "pos" }) {
  return (
    <div className="card px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`text-[17px] font-semibold tabular-nums ${tone === "bad" ? "text-bad" : tone === "pos" ? "text-pos" : ""}`}>{value}</div>
      {sub && <div className="text-[11px] text-muted tabular-nums">{sub}</div>}
    </div>
  );
}
export function Bar({ value, max, tone }: { value: number; max: number; tone?: string }) {
  const p = max > 0 ? Math.min(100, (100 * value) / max) : 0;
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-surface-2">
      <div className="h-full rounded-full" style={{ width: `${p}%`, background: tone ?? "var(--accent)" }} />
    </div>
  );
}
export function StatusPill({ s }: { s: string }) {
  const cls = s === "ok" || s === "success" || s === "done" ? "ok" : s === "failed" || s === "dead" ? "bad" : s === "pending" || s === "running" || s === "partial" || s === "retry" || s === "no_text" || s === "missing" ? "warn" : "";
  return <span className={`pill ${cls}`}>{s}</span>;
}
