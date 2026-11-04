"""Tijori Finance (tijorifinance.com) as a document source for the completeness ledger's open gaps.

Tijori hosts company-published PDFs (earnings-call transcripts, annual reports) on its own file server, linked from a
server-rendered company page. Ground rules, checked 2026-11-04 (scratchpad agent report):
  * robots.txt allows everything; the Terms of Use allow copying/storing for personal, non-commercial use only (no
    redistribution). Premium (paywalled) content is never requested.
  * >= 2 s per host (fallback.Polite: robots.txt honoured, a 401/403/429 or bot check is final for the run).
  * Precision first: a company maps to a Tijori page only by an exact name -> slug match from the sitemap, and every
    PDF must prove itself from its own text (company name, document kind, the gap's quarter or fiscal year) before
    it is stored. A rejected file is never written.
Per company: one page GET (~600 KB) lists every file; only PDFs for open ledger periods are downloaded. Each searched
gap gets a `tijori` entry in equity.data_gap.attempts; the search repeats at most every TIJORI_RETRY_DAYS.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from . import config
from .completeness import quarter_end, reported_quarter
from .fallback import CONCALL_RE, Blocked, Polite, _store_discovered, fiscal_quarter, quarter_score

log = logging.getLogger(__name__)

SITE = "https://www.tijorifinance.com"
SITEMAP_URL = f"{SITE}/sitemap.xml"
SITEMAP_CACHE = config.ROOT / "data" / "tijori" / "slugs.json"
SITEMAP_TTL_DAYS = 7
TIJORI_RETRY_DAYS = 30
GAP_S = 2.0
FILE_RE = re.compile(r"https?://files\.tijorifinance\.com/insight/india/(\d+)/(Conference%20Call|Annual%20Report)/"
                     r"(CC|AR)-([A-Za-z]{3})?(\d{2})\.pdf")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], 1)}
GENERIC = {"the", "limited", "ltd", "india", "of", "and", "company", "co", "corporation", "corp", "industries", "inds"}


# ───────── pure helpers (tests/test_tijori.py) ─────────
def name_slugs(name: str) -> List[str]:
    """Candidate Tijori slugs for a company name: 'Maruti Suzuki India Ltd' -> maruti-suzuki-india-limited, ...-ltd."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("&", " and ").replace("'", "")).strip("-")
    base = re.sub(r"-(ltd|limited)$", "", s)
    return list(dict.fromkeys([f"{base}-limited", f"{base}-ltd", base]))


def parse_sitemap(xml: str) -> Dict[str, str]:
    """slug -> company page URL (the company root pages only, not their sub-pages)."""
    out = {}
    for m in re.finditer(r"<loc>\s*(https?://[^<]*?/company/([a-z0-9-]+)/)\s*</loc>", xml):
        out.setdefault(m.group(2), m.group(1))
    return out


def parse_files(html: str) -> List[Tuple[str, str, Optional[int], int]]:
    """(kind 'CC'|'AR', url, month or None, 4-digit year) for every transcript / annual-report file on a page."""
    out, seen = [], set()
    for m in FILE_RE.finditer(html):
        url = m.group(0).replace("http://", "https://")
        if url in seen:
            continue
        seen.add(url)
        mon = MONTHS.get((m.group(4) or "").lower())
        out.append((m.group(3), url, mon, 2000 + int(m.group(5))))
    return out


def cc_quarters(month: int, year: int) -> List[date]:
    """Quarters a 'CC-MonYY' file can be: Tijori mostly names files by the results quarter's end month (CC-Mar16 is
    the Apr 2016 call on Q4 FY16) and sometimes by the call month (CC-Jan26). Most likely reading first; the text
    decides."""
    d = date(year, month, 1)
    as_qend, as_call = quarter_end(d), reported_quarter(d)
    return [as_qend, as_call] if month in (3, 6, 9, 12) else [as_call, as_qend]


def name_tokens(name: str) -> List[str]:
    """Distinctive words of a company name, in order (JSW Steel Ltd -> ['jsw', 'steel'])."""
    return [t for t in re.findall(r"[a-z0-9]+", name.lower().replace("&", " ")) if t not in GENERIC and len(t) >= 3]


TITLE_CHARS = 600            # the title block of a transcript / report cover, normalised


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower().replace("&", " ")))


def phrase(name: str) -> str:
    """The name's first two distinctive words ('ICICI Bank Ltd' -> 'icici bank', 'Eternal Ltd' -> 'eternal')."""
    return " ".join(name_tokens(name)[:2])


def _names(names) -> List[str]:
    return [names] if isinstance(names, str) else [n for n in names if n]


def title_names(text: str, names, siblings: Sequence[str] = ()) -> Optional[str]:
    """None when the document's title block names this company (current or former name): a phrase within the first
    TITLE_CHARS characters, and no sibling (a group company sharing the first word: ICICI Bank vs ICICI Prudential)
    earlier. A mention elsewhere is not enough: ICICI Prudential's transcript names 'ICICI Bank' as its promoter."""
    t = _norm(text)[:TITLE_CHARS]
    ours = [p for p in (phrase(n) for n in _names(names)) if p]
    hits = [(t.find(p), p) for p in ours if t.find(p) >= 0]
    if not hits:
        return f"title does not name {ours[0] if ours else '?'!r}"
    i, mine = min(hits)
    for s in siblings:
        j = t.find(s)
        if 0 <= j < i or (j == i and len(s) > len(mine)):
            return f"title names {s!r}, a group company"
    return None


def check_transcript(pages: Sequence[str], name, qend: date, siblings: Sequence[str] = ()) -> Optional[str]:
    """None when the text is a call transcript of `name` about the quarter ending `qend`, else why not."""
    head = "\n".join(pages[:3])
    if (why := title_names(head, name, siblings)):
        return why
    if not CONCALL_RE.search(head.lower()) or len(" ".join(pages)) < 4000:
        return "not a call transcript"
    if quarter_score(qend, head[:8000]) < 3:
        return f"quarter {qend} not stated"
    return None


def check_annual_report(pages: Sequence[str], name, fy: int, siblings: Sequence[str] = ()) -> Optional[str]:
    head = "\n".join(pages[:8])
    low = head.lower()
    # a report's cover can be all artwork: its name may first appear a few pages in, so look across the first pages
    if not any(title_names(p, name, siblings) is None for p in pages[:8]):
        return f"no early page is titled {phrase(_names(name)[0])!r}"
    if not re.search(r"annual\s+(integrated\s+)?report|integrated\s+(annual\s+)?report", low):
        return "not an annual report"
    y2 = str(fy)[2:]
    if not re.search(rf"\b{fy - 1}\s*[-–/]\s*(?:{y2}|{fy})\b|\bfy\s*'?{y2}\b|\bfy\s*{fy}\b|\b{fy}\b", low):
        return f"fiscal year {fy} not stated"
    return None


# ───────── network ─────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug_index(po: Polite) -> Dict[str, str]:
    """slug -> page URL from Tijori's sitemap, cached for SITEMAP_TTL_DAYS."""
    try:
        cached = json.loads(SITEMAP_CACHE.read_text())
        if time.time() - cached["at"] < SITEMAP_TTL_DAYS * 86400 and cached["slugs"]:
            return cached["slugs"]
    except (OSError, ValueError, KeyError):
        pass
    if not po.allowed(SITEMAP_URL):
        raise RuntimeError("robots.txt disallows the sitemap")
    st, data, _ = po.get(SITEMAP_URL, timeout=(15, 120))
    if st != 200:
        raise RuntimeError(f"sitemap http {st}")
    slugs = parse_sitemap(data.decode("utf-8", "replace"))
    SITEMAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SITEMAP_CACHE.write_text(json.dumps({"at": time.time(), "slugs": slugs}))
    return slugs


def files_robots(po: Polite) -> None:
    """robots.txt for the file host, per RFC 9309 2.3.1: it is an S3-style bucket that answers 403 for an object it
    does not have, which Polite.get would take for a block. 200 -> obeyed; any other 4xx -> no restrictions;
    5xx or unreachable -> disallow everything. A 403 on an actual PDF is still final (Polite.get raises Blocked)."""
    import urllib.robotparser
    host = "files.tijorifinance.com"
    if host in po.robots:
        return
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = po.s.get(f"https://{host}/robots.txt", timeout=(10, 20))
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
        elif 400 <= r.status_code < 500:
            rp.allow_all = True
        else:
            rp.disallow_all = True
    except Exception:
        rp.disallow_all = True
    po.robots[host] = rp


def due(attempts, now: Optional[datetime] = None) -> bool:
    last = [datetime.fromisoformat(a["at"]) for a in attempts or () if a.get("source") == "tijori" and a.get("at")]
    return not last or (now or datetime.now(timezone.utc)) - max(last) >= timedelta(days=TIJORI_RETRY_DAYS)


def discover_tijori(db, company_id: int, run_id: int, po: Optional[Polite] = None) -> dict:
    """Search Tijori for this company's open annual-report / transcript gaps (any reason) not searched there in the
    last TIJORI_RETRY_DAYS. Stores verified documents (fetch_source 'tijori'), logs an attempt on every gap."""
    from . import documents as D
    po = po or Polite(gap=GAP_S)
    files_robots(po)
    with db.trail.cursor() as cur:
        cur.execute("""select dataset, period, attempts from equity.data_gap where company_id=%s and state='open'
                       and period <> '' and dataset in ('annual_report','concall_transcript')""", (company_id,))
        todo = [(ds, p) for ds, p, a in cur.fetchall() if due(a)]
        if not todo:
            return {"searched": 0, "found": 0}
        cur.execute("select coalesce(nse_symbol, screener_slug, bse_code), name from equity.company where company_id=%s",
                    (company_id,))
        symbol, current = cur.fetchone()
        # former names (NSE name-change file, incl. names under an old symbol): Zomato Ltd for ETERNAL
        cur.execute("""select n.old_name from equity.nse_name_change n where n.symbol = %(s)s
                       or n.symbol in (select old_symbol from equity.nse_symbol_change where new_symbol = %(s)s)
                       order by n.effective_date desc""", {"s": symbol})
        name = list(dict.fromkeys([current or ""] + [r[0] for r in cur.fetchall()]))
        cur.execute("select name from equity.company where company_id <> %s and name is not null", (company_id,))
        firsts = {(name_tokens(n) or [""])[0] for n in name}
        siblings = sorted({phrase(n) for (n,) in cur.fetchall() if (name_tokens(n) or [""])[0] in firsts}
                          - {phrase(n) for n in name})
    results: Dict[Tuple[str, str], str] = {}
    found = 0
    try:
        slugs = slug_index(po)
        page = next((slugs[s] for n in name for s in name_slugs(n) if s in slugs), None)
        if page is None or not po.allowed(page):
            results = {g: (f"no Tijori page for {name!r}" if page is None else "robots.txt disallows the page")
                       for g in todo}
        else:
            st, html, _ = po.get(page, timeout=(15, 90))
            files = parse_files(html.decode("utf-8", "replace")) if st == 200 else []
            for ds, period in todo:
                results[(ds, period)] = f"page {page}: no file for {period}" if st == 200 else f"page http {st}"
            used = set()
            for ds, period in todo:
                if ds == "annual_report":
                    fy = int(period[2:])
                    cands = [(u, None, fy) for k, u, _, y in files if k == "AR" and y == fy]
                else:
                    qend = date.fromisoformat(period)
                    cands = [(u, qend, None) for k, u, m, y in files if k == "CC" and m and qend in cc_quarters(m, y)]
                for url, qend, fy in cands:
                    if url in used or not po.allowed(url):
                        continue
                    st, data, _ = po.get(url, binary=True)
                    if st != 200:
                        results[(ds, period)] = f"{url}: http {st}"
                        continue
                    fq = None if ds == "annual_report" else fiscal_quarter(qend)
                    label = f"Annual Report {fy} (tijori)" if fq is None else f"Q{fq[0]} FY{str(fq[1])[2:]} (tijori)"
                    chk = ((lambda pg, f=fy: check_annual_report(pg, name, f, siblings)) if ds == "annual_report"
                           else (lambda pg, q=qend: check_transcript(pg, name, q, siblings)))
                    r = D.process_bytes(data, url, symbol, ds, label, check=chk)
                    if r["status"] != "ok":
                        results[(ds, period)] = f"{url}: {r['status']} ({r.get('error', '')})"[:240]
                        continue
                    entry = {"source": "tijori", "url": url, "at": _now(), "result": f"verified for {period}"}
                    ddate = None if ds == "annual_report" else qend + timedelta(days=1)
                    with db.trail.cursor() as cur:
                        _store_discovered(cur, company_id, ds, url, label, fy if fq is None else fq[1], ddate, r,
                                          entry, fetch_source="tijori")
                    used.add(url)
                    found += 1
                    results[(ds, period)] = f"found {url}"
                    break
    except Blocked as e:
        results = {g: f"blocked: {e}" for g in todo if not results.get(g, "").startswith("found")} | \
                  {g: v for g, v in results.items() if v.startswith("found")}
    with db.trail.cursor() as cur:
        for (ds, period), res in results.items():
            cur.execute("""update equity.data_gap set attempts = attempts || %s::jsonb, updated_at = now()
                           where company_id=%s and dataset=%s and period=%s""",
                        (json.dumps([{"source": "tijori", "at": _now(), "result": res[:240]}]), company_id, ds, period))
    log.info("%s: tijori searched %d gaps, found %d", symbol, len(todo), found)
    return {"searched": len(todo), "found": found}


LOCK_NAME = "tijori"             # data/.tijori.lock: one Tijori client at a time (batch CLI or the concalls role)


def companies_due(db, limit: Optional[int] = None) -> List[Tuple[int, str]]:
    """Ranked companies with open report / transcript gaps that Tijori has not searched in TIJORI_RETRY_DAYS."""
    with db.trail.cursor() as cur:
        cur.execute("""select c.company_id, c.nse_symbol from equity.company c
                       join equity.universe u on u.company_id = c.company_id and u.universe_code = 'TOP1000_MCAP'
                         and u.as_of = (select max(as_of) from equity.universe where universe_code = 'TOP1000_MCAP')
                       where exists (select 1 from equity.data_gap g where g.company_id = c.company_id
                             and g.state = 'open' and g.period <> '' and g.dataset in ('annual_report','concall_transcript')
                             and not exists (select 1 from jsonb_array_elements(g.attempts) e where e->>'source' = 'tijori'
                                 and (e->>'at')::timestamptz > now() - make_interval(days => %s)))
                       order by u.rank limit %s""", (TIJORI_RETRY_DAYS, limit))
        return cur.fetchall()


def tijori_cli(limit: Optional[int] = None) -> int:
    from .db import DB
    from .lake import lake_url
    from .sources import single_instance
    with single_instance(LOCK_NAME) as ok:
        if not ok:
            print("another Tijori run holds the lock; exiting")
            return 0
        db = DB(lake_url())
        po = Polite(gap=GAP_S)
        run_id = db.start_run("tijori", {"limit": limit})
        tot = {"companies": 0, "searched": 0, "found": 0, "failed": 0}
        try:
            for cid, sym in companies_due(db, limit):
                try:
                    r = discover_tijori(db, cid, run_id, po)
                    tot["searched"] += r["searched"]
                    tot["found"] += r["found"]
                except Exception as e:
                    tot["failed"] += 1
                    log.warning("tijori %s: %s: %s", sym, type(e).__name__, str(e)[:200])
                tot["companies"] += 1
                if tot["companies"] % 25 == 0:
                    log.info("tijori: %s", tot)
        finally:
            db.finish_run(run_id, "success" if not tot["failed"] else "partial", tot)
            db.close()
        print(tot)
    return 0
