// Number formatting: Indian digit grouping (₹ 1,23,456 Cr), tabular figures come from CSS.
const cache = new Map<number, Intl.NumberFormat>();
function nf(d: number) {
  let f = cache.get(d);
  if (!f) {
    f = new Intl.NumberFormat("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
    cache.set(d, f);
  }
  return f;
}
export const DASH = "–";
export const isNum = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);
export function fmt(n: number | null | undefined, d = 2): string {
  return isNum(n) ? nf(d).format(n) : DASH;
}
export function cr(n: number | null | undefined, d = 0): string {
  return isNum(n) ? `₹ ${nf(d).format(n)} Cr` : DASH;
}
export function rs(n: number | null | undefined, d = 2): string {
  return isNum(n) ? `₹ ${nf(d).format(n)}` : DASH;
}
export function pct(n: number | null | undefined, d = 1, sign = false): string {
  if (!isNum(n)) return DASH;
  return `${sign && n > 0 ? "+" : ""}${nf(d).format(n)}%`;
}
export function byUnit(v: number | null | undefined, unit: string): string {
  if (!isNum(v)) return DASH;
  switch (unit) {
    case "cr": return nf(Math.abs(v) < 100 && !Number.isInteger(v) ? 2 : 0).format(v);
    case "pct": return nf(1).format(v) + "%";
    case "rs": return nf(2).format(v);
    case "days": return nf(0).format(v);
    case "count": return nf(0).format(v);
    case "x": return nf(2).format(v) + "x";
    default: return nf(2).format(v);
  }
}
export const UNIT_LABEL: Record<string, string> = { cr: "₹ Cr", pct: "%", rs: "₹", days: "days", count: "count", x: "x" };
export function date(d: string | null | undefined): string {
  if (!d) return DASH;
  const t = new Date(d.length === 10 ? d + "T00:00:00" : d);
  if (isNaN(+t)) return d;
  return t.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}
export function dateTime(d: string | null | undefined): string {
  if (!d) return DASH;
  const t = new Date(d);
  return isNaN(+t) ? d : t.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}
export function bytes(n: number | null | undefined): string {
  if (!isNum(n)) return DASH;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + " KB";
  return n + " B";
}
export const DOC_LABEL: Record<string, string> = { annual_report: "Annual report", concall_transcript: "Concall", concall_ppt: "Concall PPT" };
