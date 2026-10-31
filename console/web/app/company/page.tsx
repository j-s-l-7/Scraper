"use client";
import { Suspense } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import CompanyView from "@/components/company/CompanyView";

function Inner() {
  const sp = useSearchParams();
  const router = useRouter();
  const sym = (sp.get("s") || "").toUpperCase();
  const tab = sp.get("tab") || "overview";
  if (!sym) return <div className="text-muted">No symbol. <Link href="/" className="text-brand underline">Back to securities</Link></div>;
  return <CompanyView sym={sym} tab={tab} setTab={(t) => router.replace(`/company/?s=${sym}&tab=${t}`)} />;
}
export default function Page() { return <Suspense><Inner /></Suspense>; }
