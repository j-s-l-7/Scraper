"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

const NAV = [
  { href: "/", label: "Securities", key: "s" },
  { href: "/library/", label: "Library", key: "l" },
  { href: "/ops/", label: "Ops", key: "o" },
];

export default function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const path = usePathname() || "/";
  const [help, setHelp] = useState(false);
  const gPending = useRef<number | null>(null);

  const toggleTheme = useCallback(() => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("ec-theme", next); } catch {}
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
      if (e.key === "Escape") { setHelp(false); (t as HTMLElement)?.blur?.(); return; }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (gPending.current) {
        window.clearTimeout(gPending.current); gPending.current = null;
        const dest = NAV.find((n) => n.key === e.key);
        if (dest) { e.preventDefault(); router.push(dest.href); }
        return;
      }
      if (e.key === "g") { gPending.current = window.setTimeout(() => (gPending.current = null), 1200); return; }
      if (e.key === "/") {
        e.preventDefault();
        const el = document.querySelector<HTMLInputElement>("[data-search]");
        if (el) el.focus(); else router.push("/library/");
      } else if (e.key === "?") setHelp((h) => !h);
      else if (e.key === "t") toggleTheme();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router, toggleTheme]);

  return (
    <>
      <header className="sticky top-0 z-30 border-b border-line bg-surface/95 backdrop-blur">
        <div className="mx-auto flex h-11 max-w-[1600px] items-center gap-5 px-4">
          <Link href="/" className="font-semibold tracking-tight text-brand">Equity Console</Link>
          <nav className="flex gap-1">
            {NAV.map((n) => {
              const on = n.href === "/" ? path === "/" || path.startsWith("/company") : path.startsWith(n.href.slice(0, -1));
              return (
                <Link key={n.href} href={n.href} className={`rounded-md px-2.5 py-1 text-[13px] ${on ? "bg-surface-2 font-medium" : "text-muted hover:text-ink"}`}>
                  {n.label}
                </Link>
              );
            })}
          </nav>
          <div className="ml-auto flex items-center gap-2 text-muted">
            <button className="btn" onClick={() => setHelp(true)} aria-label="Keyboard shortcuts"><span className="kbd">?</span> shortcuts</button>
            <button className="btn" onClick={toggleTheme} aria-label="Toggle theme">theme</button>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-[1600px] flex-1 px-4 py-4">{children}</main>
      {help && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/40" onClick={() => setHelp(false)}>
          <div className="card w-[380px] p-5" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 font-semibold">Keyboard shortcuts</div>
            <dl className="grid grid-cols-[90px_1fr] gap-y-2 text-[13px]">
              {[["/", "Focus search"], ["g s", "Securities"], ["g l", "Library"], ["g o", "Ops"], ["j / k", "Next / previous row or page"], ["Enter", "Open selected row"], ["t", "Toggle theme"], ["Esc", "Close / blur"]].map(([k, v]) => (
                <div key={k} className="contents"><dt><span className="kbd">{k}</span></dt><dd className="text-muted">{v}</dd></div>
              ))}
            </dl>
          </div>
        </div>
      )}
    </>
  );
}
