"""Fallback sources for documents whose original url is dead (404) or failed.

Chain per document: wayback (Internet Archive public API) -> company_site (the company's own public website).
Rules: our normal UA only, no header/cookie spoofing; robots.txt is respected for every crawled site; a 401/403/429
or a bot-check page from a host is final for that host (no retries, no workarounds); >=1s between requests per host;
no use of nseindia.com / bseindia.com internal APIs.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import threading
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse, urldefrag

import requests

from . import config
from .http import MAX_GET_S
from .rules import BLOCK_RE, DocNotFound

log = logging.getLogger(__name__)

MIN_GAP = 1.0                 # seconds between requests to one host
MAX_SITEMAPS = 20
MAX_PAGES = 60
MAX_DEPTH = 3
MAX_PDF_BYTES = 150 * 1024 * 1024
NAV_RE = re.compile(r"investor|financial|analyst|earnings|results|transcript|annual", re.I)
CONCALL_RE = re.compile(r"transcript|concall|con-call|earnings[\s_%20-]*call|conference[\s_%20-]*call|"
                        r"earning[\s_%20-]*call|call[\s_-]*script|prepared[\s_-]*remarks", re.I)
PPT_RE = re.compile(r"presentation|investor[\s_%20-]*deck|\bppt\b|earnings[\s_%20-]*deck|results[\s_%20-]*deck", re.I)
AR_RE = re.compile(r"annual[\s_%20-]*report", re.I)
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]


class Blocked(Exception):
    """The host refused us (401/403/429 or a bot-check page): final for that host."""


# ───────── polite, robots-aware fetching ─────────
class Polite:
    def __init__(self, gap: float = MIN_GAP):
        self.gap = gap
        self.last: Dict[str, float] = {}
        self.blocked: set = set()
        self.robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self.lock = threading.Lock()
        self.crawl_cache: Dict[str, List[Tuple[str, str]]] = {}
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": config.USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

    def _wait(self, host: str) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.last.get(host, 0) + self.gap)
            self.last[host] = start
        if start > now:
            time.sleep(start - now)

    def get(self, url: str, timeout=(15, 60), binary: bool = False, max_bytes: int = MAX_PDF_BYTES, headers=None):
        """Returns (status, content_bytes, content_type). Raises Blocked on 401/403/429 or bot-check pages."""
        host = urlparse(url).netloc.lower()
        if host in self.blocked:
            raise Blocked(f"{host} blocked earlier in this run")
        last: Optional[Exception] = None
        for attempt in (1, 2):
            self._wait(host)
            try:
                t0 = time.monotonic()
                r = self.s.get(url, timeout=timeout, stream=True, headers=headers)
                try:
                    st = r.status_code
                    if st in (401, 403, 429):
                        self.blocked.add(host)
                        raise Blocked(f"HTTP {st} from {host}")
                    if st >= 500:
                        last = RuntimeError(f"HTTP {st}")
                        continue
                    body, n = [], 0
                    for chunk in r.iter_content(1 << 16):        # small reads, so the MAX_GET_S check runs often
                        body.append(chunk)
                        n += len(chunk)
                        if n > max_bytes:
                            return 413, b"", r.headers.get("Content-Type", "")
                        if time.monotonic() - t0 > MAX_GET_S:   # a server trickling bytes: give up, no retry
                            raise TimeoutError(f"still downloading after {MAX_GET_S // 60} min: {url}")
                    data = b"".join(body)
                    ct = r.headers.get("Content-Type", "")
                finally:
                    r.close()
                if st == 200 and "html" in ct.lower() and BLOCK_RE.search(data[:6000].decode("utf-8", "ignore")):
                    self.blocked.add(host)
                    raise Blocked(f"bot-check page from {host}")
                return st, data, ct
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
        raise ConnectionError(f"{type(last).__name__}: {last}")

    def allowed(self, url: str) -> bool:
        u = urlparse(url)
        host = u.netloc.lower()
        if host not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                st, data, _ = self.get(f"{u.scheme}://{host}/robots.txt", timeout=(10, 20))
                if st == 200:
                    rp.parse(data.decode("utf-8", "ignore").splitlines())
                elif st in (401, 403):
                    rp.disallow_all = True
                else:
                    rp.allow_all = True
            except Blocked:
                rp.disallow_all = True
            except Exception:
                rp.allow_all = True
            self.robots[host] = rp
        return self.robots[host].can_fetch(config.USER_AGENT, url)

    def sitemaps(self, host: str, scheme: str = "https") -> List[str]:
        self.allowed(f"{scheme}://{host}/")
        rp = self.robots.get(host)
        found = list(rp.site_maps() or []) if rp else []
        return found or [f"{scheme}://{host}/sitemap.xml"]


# ───────── wayback ─────────
def parse_wayback_available(js) -> Optional[Tuple[str, str]]:
    """wayback/available JSON -> (timestamp, snapshot_url) for a 200 snapshot, else None."""
    try:
        c = js["archived_snapshots"]["closest"]
        if c.get("available") and str(c.get("status", "200")) == "200":
            return str(c["timestamp"]), c["url"]
    except (KeyError, TypeError, AttributeError):
        pass
    return None


def parse_cdx(js) -> Optional[str]:
    """CDX json (first row = header) -> newest timestamp with statuscode 200 (limit=-5 returns the last 5)."""
    if not isinstance(js, list) or len(js) < 2:
        return None
    head, rows = js[0], js[1:]
    try:
        ti, si = head.index("timestamp"), head.index("statuscode")
    except ValueError:
        ti, si = 1, 4
    good = [r[ti] for r in rows if len(r) > max(ti, si) and str(r[si]) == "200"]
    return max(good) if good else None


def wayback_raw_url(timestamp: str, url: str) -> str:
    return f"https://web.archive.org/web/{timestamp}id_/{url}"


def wayback_fetch(po: Polite, url: str, log_: list) -> Optional[bytes]:
    ts = None
    try:
        st, data, _ = po.get("https://archive.org/wayback/available?url=" + quote(url, safe=""), timeout=(15, 40))
        hit = parse_wayback_available(json.loads(data)) if st == 200 else None
        if hit:
            ts = hit[0]
            log_.append({"source": "wayback", "url": "available", "result": f"snapshot {ts}"})
        else:
            log_.append({"source": "wayback", "url": "available", "result": f"no snapshot (HTTP {st})"})
        if ts is None:
            st, data, _ = po.get("http://web.archive.org/cdx/search/cdx?url=" + quote(url, safe="") +
                                 "&output=json&filter=statuscode:200&limit=-5", timeout=(15, 60))
            ts = parse_cdx(json.loads(data)) if st == 200 and data.strip() else None
            log_.append({"source": "wayback", "url": "cdx", "result": f"snapshot {ts}" if ts else f"no capture (HTTP {st})"})
        if ts is None:
            return None
        raw = wayback_raw_url(ts, url)
        st, data, _ = po.get(raw)
        log_.append({"source": "wayback", "url": raw, "result": f"HTTP {st}, {len(data)} bytes"})
        return data if st == 200 and data else None
    except Blocked as e:
        log_.append({"source": "wayback", "url": url, "result": f"blocked: {e}"})
    except Exception as e:
        log_.append({"source": "wayback", "url": url, "result": f"error {type(e).__name__}: {str(e)[:150]}"})
    return None


# ───────── company site ─────────
def parse_sitemap(text: str) -> Tuple[bool, List[str]]:
    """-> (is_index, [loc urls])"""
    locs = [re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.strip()).strip() for m in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.S)]
    return "<sitemapindex" in text[:2000].lower() or "<sitemapindex" in text.lower(), locs


def extract_links(html: str, base: str) -> List[Tuple[str, str]]:
    """-> [(absolute_url, anchor_text)] (fragments dropped, mailto/javascript skipped)."""
    out = []
    for m in re.finditer(r"<a\b[^>]*?href\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))[^>]*>(.*?)</a>", html, re.S | re.I):
        href = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(4))).strip()
        out.append((urldefrag(urljoin(base, href))[0], text))
    return out


def host_family(host: str) -> str:
    return re.sub(r"^www\d*\.", "", host.lower())


def same_site(url: str, family: str) -> bool:
    h = urlparse(url).netloc.lower()
    return host_family(h) == family or h.endswith("." + family)


def is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith((".pdf", ".zip", ".docx", ".pptx", ".txt", ".ppt", ".doc"))


def crawl_company(po: Polite, website: Optional[str], original_url: str, notes: list) -> List[Tuple[str, str]]:
    """Collect candidate (pdf_url, context_text) pairs from the company's own site. Stops quietly if blocked."""
    base = website or original_url
    if base and not base.startswith("http"):
        base = "https://" + base
    u = urlparse(base)
    host, family = u.netloc.lower(), host_family(u.netloc)
    if not host:
        return []
    scheme = "https"
    cands: Dict[str, str] = {}
    try:
        # 1) sitemaps
        queue, seen_maps = po.sitemaps(host, scheme), 0
        while queue and seen_maps < MAX_SITEMAPS:
            sm = queue.pop(0)
            if not same_site(sm, family) or not po.allowed(sm):
                continue
            seen_maps += 1
            st, data, _ = po.get(sm, timeout=(15, 60))
            if st != 200:
                continue
            if sm.endswith(".gz") or data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except OSError:
                    continue
            is_index, locs = parse_sitemap(data.decode("utf-8", "ignore"))
            if is_index:
                queue.extend(locs)
            else:
                for loc in locs:
                    if is_pdf_url(loc) and same_site(loc, family):
                        cands.setdefault(loc, loc)
        notes.append({"source": "company_site", "url": f"{scheme}://{host}", "result": f"sitemaps read: {seen_maps}, pdfs {len(cands)}"})
        # 2) shallow crawl
        frontier, seen, pages = [(f"{scheme}://{host}/", 0)], set(), 0
        while frontier and pages < MAX_PAGES:
            url, depth = frontier.pop(0)
            if url in seen or not same_site(url, family) or not po.allowed(url):
                continue
            seen.add(url)
            st, data, ct = po.get(url, timeout=(15, 45))
            pages += 1
            if st != 200 or "html" not in ct.lower():
                continue
            links = extract_links(data.decode("utf-8", "ignore"), url)
            page_has_pdf = any(is_pdf_url(h) and same_site(h, family) for h, _ in links)
            for href, text in links:
                if not same_site(href, family):
                    continue
                if page_has_pdf and not is_pdf_url(href) and is_listing_link(href, text, url):
                    frontier.append((href, depth))      # pagination / year tabs of a PDF listing page: same depth
                elif is_pdf_url(href):
                    cands.setdefault(href, text)
                    if text and text not in cands[href]:
                        cands[href] = f"{text} {cands[href]}"
                elif CONCALL_RE.search(href + " " + text) and not is_pdf_url(href):
                    cands.setdefault(href, text)            # HTML transcript page
                    if depth < MAX_DEPTH:
                        frontier.append((href, depth + 1))
                elif depth < MAX_DEPTH and NAV_RE.search(href + " " + text):
                    frontier.append((href, depth + 1))
        notes.append({"source": "company_site", "url": f"{scheme}://{host}", "result": f"crawled {pages} pages, {len(cands)} pdf links"})
    except Blocked as e:
        notes.append({"source": "company_site", "url": f"{scheme}://{host}", "result": f"blocked, stopped: {e}"})
    except Exception as e:
        notes.append({"source": "company_site", "url": f"{scheme}://{host}", "result": f"error {type(e).__name__}: {str(e)[:150]}"})
    return list(cands.items())


LISTING_Q_RE = re.compile(r"(^|[?&;])(year|fy|page|pg|p|pageno|financialyear|fin_year)=", re.I)
LISTING_TEXT_RE = re.compile(r"^(\d{4}([-/]\d{2,4})?|fy ?\d{2,4}|next|older|more|load more|[<>\u2039\u203a\u00ab\u00bb]+|\d{1,2})$", re.I)


def is_listing_link(href: str, text: str, current: str) -> bool:
    """Pagination / year-tab links of a listing page: same path with a year/page query, or a bare year/page label."""
    h, c = urlparse(href), urlparse(current)
    if h.path == c.path and h.query and h.query != c.query and LISTING_Q_RE.search("?" + h.query):
        return True
    return bool(LISTING_TEXT_RE.match((text or "").strip())) and h.path.rsplit("/", 1)[0] == c.path.rsplit("/", 1)[0]


def cached_crawl(po: Polite, website: Optional[str], original_url: str, notes: list) -> List[Tuple[str, str]]:
    base = website or original_url
    key = host_family(urlparse(base if "//" in (base or "") else "https://" + (base or "")).netloc)
    if key not in po.crawl_cache:
        po.crawl_cache[key] = crawl_company(po, website, original_url, notes)
    return po.crawl_cache[key]


# ───────── matching ─────────
def quarter_of_call(month: int, year: int) -> Tuple[int, int]:
    """Results quarter discussed in a call held in `month`/`year` -> (quarter 1-4 of the fiscal year, FY end year)."""
    if month in (7, 8, 9):
        return 1, year + 1
    if month in (10, 11, 12):
        return 2, year + 1
    if month in (1, 2, 3):
        return 3, year
    return 4, year


def _norm(s: str) -> str:
    from urllib.parse import unquote
    return re.sub(r"[^a-z0-9]+", " ", unquote(s).lower()).strip()


def match_score(doc_type: str, label: Optional[str], fiscal_year: Optional[int], doc_date, text: str) -> float:
    """Score how well a pdf url/anchor text matches a document. >= 3 is accepted."""
    t = " " + _norm(text) + " "
    t_compact = t.replace(" ", "")
    if doc_type == "annual_report":
        if not AR_RE.search(_norm(text).replace(" ", "-")) and "annual report" not in t and "annualreport" not in t_compact:
            return 0
        if not fiscal_year:
            return 0
        y4, y2 = str(fiscal_year), str(fiscal_year)[2:]
        prev4, prev2 = str(fiscal_year - 1), str(fiscal_year - 1)[2:]
        sc = 0.0
        if re.search(rf"\b{prev4} {y2}\b|\b{prev4} {y4}\b|\b{prev2} {y2}\b|\b{prev4}{y2}\b", t) or f"{prev4}{y2}" in t_compact:
            sc += 4
        elif re.search(rf"\b{y4}\b", t):
            sc += 3
        wrong = [str(y) for y in range(fiscal_year - 12, fiscal_year + 3) if y != fiscal_year and y != fiscal_year - 1]
        if sc and any(re.search(rf"\b{w}\b", t) for w in wrong) and not re.search(rf"\b{y4}\b", t):
            sc -= 2
        return sc
    # concall transcripts / presentations
    kw = PPT_RE if doc_type == "concall_ppt" else CONCALL_RE
    if not kw.search(text.lower().replace("%20", " ")) and not kw.search(_norm(text)):
        return 0
    sc = 0.0
    if doc_date is not None:
        m, y = doc_date.month, doc_date.year
        full, abbr = MONTHS[m - 1], MONTHS[m - 1][:3]
        if re.search(rf"\b({full}|{abbr})\b {y}\b|\b{y} ({full}|{abbr})\b|\b({full}|{abbr}) {str(y)[2:]}\b", t):
            sc += 3
        q, fy = quarter_of_call(m, y)
        fy2 = str(fy)[2:]
        if re.search(rf"\bq{q}\b ?(fy)? ?({fy}|{fy2})\b|\bq{q}fy({fy}|{fy2})\b|\b({fy}|{fy2}) q{q}\b|\bfy ?({fy}|{fy2}) q{q}\b", t) \
                or f"q{q}fy{fy2}" in t_compact or f"q{q}fy{fy}" in t_compact:
            sc += 3
        if sc == 0 and re.search(rf"\b{y}\b", t):
            sc += 0.5
    return sc


def best_match(cands: List[Tuple[str, str]], doc_type: str, label, fiscal_year, doc_date, used=()) -> Optional[Tuple[str, float]]:
    best = None
    for url, ctx in cands:
        if url in used:
            continue
        sc = match_score(doc_type, label, fiscal_year, doc_date, f"{urlparse(url).path} {ctx}")
        if sc >= 3 and (best is None or sc > best[1]):
            best = (url, sc)
    return best


# ───────── per-company driver ─────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def retry_company(db, company_id: int, run_id: int, kind: str, po: Optional[Polite] = None) -> dict:
    """Run the fallback chain for every missing/failed document of the company's kind. Never raises for a single
    document; returns counts by fetch_source."""
    from . import documents as D
    po = po or Polite()
    doc_type = D.DOC_TYPE_OF[kind]
    stats = {"tried": 0, "recovered": 0, "by_source": {}, "still_missing": 0, "seconds": 0.0}
    t0 = time.monotonic()
    with db.trail.cursor() as cur:
        cur.execute("select coalesce(nse_symbol, screener_slug, bse_code), website from equity.company where company_id=%s",
                    (company_id,))
        symbol, website = cur.fetchone()
        cur.execute("""select url, period_label, fiscal_year, doc_date, status, attempts_log from equity.document
                       where company_id=%s and doc_type=%s and status in ('missing','failed','no_text') order by doc_date desc nulls last""",
                    (company_id, doc_type))
        todo = cur.fetchall()
    site_cands: Optional[List[Tuple[str, str]]] = None
    site_notes: list = []
    used: set = set()
    for url, label, fy, ddate, status, old_log in todo:
        stats["tried"] += 1
        log_: list = []
        res = None
        source = None
        if status in ("failed", "no_text"):                  # transient failure / OCR now possible: retry the original
            try:
                raw = D.download(url, tries=2)
                res, source = D.process_bytes(raw, url, symbol, doc_type, label), "original_retry"
                log_.append({"source": "original_retry", "url": url, "result": res["status"]})
            except Exception as e:
                log_.append({"source": "original_retry", "url": url, "result": f"{type(e).__name__}: {str(e)[:150]}"})
        fetched = url
        if res is None or res["status"] == "failed":
            data = wayback_fetch(po, url, log_)
            if data:
                try:
                    r = D.process_bytes(data, url, symbol, doc_type, label)
                    if r["status"] in ("ok", "no_text"):
                        res, source, fetched = r, "wayback", url
                    log_.append({"source": "wayback", "url": url, "result": f"extract {r['status']}"})
                except Exception as e:
                    log_.append({"source": "wayback", "url": url, "result": f"unusable: {type(e).__name__}: {str(e)[:120]}"})
        if res is None or res["status"] == "failed":
            data = commoncrawl_fetch(po, url, log_)
            if data:
                try:
                    r = D.process_bytes(data, url, symbol, doc_type, label)
                    log_.append({"source": "commoncrawl", "url": url, "result": f"extract {r['status']}"})
                    if r["status"] in ("ok", "no_text"):
                        res, source, fetched = r, "commoncrawl", url
                except Exception as e:
                    log_.append({"source": "commoncrawl", "url": url, "result": f"unusable: {type(e).__name__}: {str(e)[:120]}"})
        if res is None or res["status"] == "failed":
            if site_cands is None:
                site_cands = cached_crawl(po, website, url, site_notes)
                log_.extend(site_notes)
            m = best_match(site_cands, doc_type, label, fy, ddate, used)
            if m is None:
                log_.append({"source": "company_site", "url": None, "result": "no matching pdf"})
            else:
                try:
                    st, data, _ = po.get(m[0])
                    if st != 200:
                        raise DocNotFound(f"HTTP {st}")
                    r = D.process_bytes(data, m[0], symbol, doc_type, label)
                    log_.append({"source": "company_site", "url": m[0], "result": f"score {m[1]}, {r['status']}"})
                    if r["status"] in ("ok", "no_text"):
                        res, source, fetched = r, "company_site", m[0]
                        used.add(m[0])
                except Blocked as e:
                    log_.append({"source": "company_site", "url": m[0], "result": f"blocked: {e}"})
                except Exception as e:
                    log_.append({"source": "company_site", "url": m[0], "result": f"{type(e).__name__}: {str(e)[:120]}"})
        if (res is None or res["status"] != "ok") and doc_type == "concall_transcript":
            a = audio_fallback(db, po, company_id, symbol, label, ddate, log_)
            if a:
                pages, mins = a
                import hashlib
                sha = hashlib.sha256("\n".join(pages).encode()).hexdigest()
                path = D.write_text(symbol, doc_type, label, sha, pages)
                res = {"status": "ok", "sha": sha, "page_count": len(pages), "char_count": sum(map(len, pages)),
                       "text_path": str(path.relative_to(config.ROOT)), "extractor": "openai-transcribe"}
                source, fetched = "audio", url
        for e in log_:
            e.setdefault("at", _now())
        full_log = json.dumps((old_log or []) + log_)
        with db.trail.cursor() as cur:
            if res is not None and res["status"] in ("ok", "no_text"):
                D._set_status(cur, company_id, doc_type, url, res["status"],
                              **{k: v for k, v in res.items() if k != "status"})
                cur.execute("""update equity.document set fetch_source=%s, fetched_url=%s, attempts_log=%s::jsonb
                               where company_id=%s and doc_type=%s and url=%s""",
                            (source, fetched, full_log, company_id, doc_type, url))
                stats["recovered"] += 1
                stats["by_source"][source] = stats["by_source"].get(source, 0) + 1
                log.info("%s %s %s: recovered via %s", symbol, doc_type, label, source)
            else:
                cur.execute("update equity.document set attempts_log=%s::jsonb where company_id=%s and doc_type=%s and url=%s",
                            (full_log, company_id, doc_type, url))
                stats["still_missing"] += 1
    stats["seconds"] = round(time.monotonic() - t0, 1)
    return stats


# ───────── common crawl ─────────
def parse_cc_index(text: str) -> List[dict]:
    """Common Crawl index response: one JSON object per line. Keeps 200 captures, newest first."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if str(o.get("status")) == "200" and o.get("filename") and o.get("offset") is not None:
            out.append(o)
    return sorted(out, key=lambda o: o.get("timestamp", ""), reverse=True)


def parse_collinfo(js, n: int = 6) -> List[str]:
    """collinfo.json -> ids of the latest n indexes (CC-MAIN-...)."""
    try:
        ids = [c["id"] for c in js if str(c.get("id", "")).startswith("CC-MAIN")]
    except (TypeError, KeyError):
        return []
    return sorted(ids, reverse=True)[:n]


def warc_payload(data: bytes) -> Optional[bytes]:
    """HTTP payload of a (gzipped) WARC response record."""
    try:
        from warcio.archiveiterator import ArchiveIterator
    except ImportError:
        return None
    import io
    for rec in ArchiveIterator(io.BytesIO(data)):
        if rec.rec_type == "response":
            return rec.content_stream().read()
    return None


def commoncrawl_fetch(po: Polite, url: str, log_: list) -> Optional[bytes]:
    try:
        st, data, _ = po.get("https://index.commoncrawl.org/collinfo.json", timeout=(15, 40))
        ids = parse_collinfo(json.loads(data)) if st == 200 else []
        if not ids:
            log_.append({"source": "commoncrawl", "url": "collinfo", "result": f"no index list (HTTP {st})"})
            return None
        t_end = time.monotonic() + 45                       # per-document time budget for the slow CC index
        for cid in ids:
            if time.monotonic() > t_end:
                log_.append({"source": "commoncrawl", "url": url, "result": "time budget (45s) exhausted"})
                return None
            q = f"https://index.commoncrawl.org/{cid}-index?url={quote(url, safe='')}&output=json"
            st, data, _ = po.get(q, timeout=(15, 90))
            if st != 200:
                continue
            hits = parse_cc_index(data.decode("utf-8", "ignore"))
            if not hits:
                continue
            h = hits[0]
            off, ln = int(h["offset"]), int(h["length"])
            st2, blob, _ = po.get("https://data.commoncrawl.org/" + h["filename"], timeout=(15, 120),
                                  headers={"Range": f"bytes={off}-{off + ln - 1}"})
            payload = warc_payload(blob) if st2 in (200, 206) else None
            log_.append({"source": "commoncrawl", "url": f"{cid}:{h['filename']}", "result":
                         f"HTTP {st2}, payload {len(payload) if payload else 0} bytes"})
            if payload:
                return payload
        log_.append({"source": "commoncrawl", "url": url, "result": f"no capture in {len(ids)} indexes"})
    except Blocked as e:
        log_.append({"source": "commoncrawl", "url": url, "result": f"blocked: {e}"})
    except Exception as e:
        log_.append({"source": "commoncrawl", "url": url, "result": f"error {type(e).__name__}: {str(e)[:150]}"})
    return None


# ───────── quarters ─────────
def reported_quarter_end(d: date) -> date:
    """Quarter end of the results a call on `d` discusses: the calendar quarter before d's quarter."""
    first = date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    return first - timedelta(days=1)


def quarter_end_of(d: date) -> date:
    m = 3 * ((d.month - 1) // 3) + 3
    nxt = date(d.year + (m == 12), m % 12 + 1, 1)
    return nxt - timedelta(days=1)


def expected_quarters(today: Optional[date] = None, lag_days: int = 45) -> List[date]:
    today = today or date.today()
    out, q = [], date(2016, 6, 30)
    while q <= today - timedelta(days=lag_days):
        out.append(q)
        q = quarter_end_of(q + timedelta(days=45))
    return out


def fiscal_quarter(qend: date) -> Tuple[int, int]:
    """(fiscal quarter 1-4, FY end year) of a calendar quarter end (FY runs Apr-Mar)."""
    m = qend.month
    q = {6: 1, 9: 2, 12: 3, 3: 4}[m]
    return q, qend.year + (1 if m in (6, 9, 12) else 0)


def completeness(expected: List, found) -> Tuple[int, int, list]:
    """(expected, found, missing) for any ordered expected list."""
    fs = set(found)
    missing = [e for e in expected if e not in fs]
    return len(expected), len(expected) - len(missing), missing


def quarter_score(qend: date, text: str) -> float:
    t = " " + _norm(text) + " "
    tc = t.replace(" ", "")
    if not CONCALL_RE.search(text.lower().replace("%20", " ")) and not CONCALL_RE.search(_norm(text)):
        return 0
    q, fy = fiscal_quarter(qend)
    fy2 = str(fy)[2:]
    sc = 0.0
    if f"q{q}fy{fy2}" in tc or f"q{q}fy{fy}" in tc or re.search(rf"\bq{q}\b ?(fy)? ?({fy}|{fy2})\b|\b(fy) ?({fy}|{fy2}) q{q}\b", t):
        sc += 3
    cm = qend
    for k in (1, 2, 3):                                    # call months: the 3 months after the quarter end
        mm = qend.month + k
        yy = qend.year + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        full = MONTHS[mm - 1]
        if re.search(rf"\b({full}|{full[:3]})\b {yy}\b|\b{yy} ({full}|{full[:3]})\b|\b({full}|{full[:3]}) {str(yy)[2:]}\b", t):
            sc += 3
            break
    return sc


# ───────── audio (last resort for concalls) ─────────
AUDIO_EXT = (".mp3", ".m4a", ".wav", ".mp4")
STREAMING = ("youtube.", "youtu.be", "vimeo.", "spotify.", "soundcloud.", "dailymotion.", "facebook.", "twitter.",
             "instagram.", "twitch.", "x.com", "podcasts.apple.", "gaana.", "jiosaavn.", "wynk.")
AUDIO_USD_PER_MIN = 0.006
CHUNK_SECONDS = 1200
MAX_AUDIO_BYTES = 24 * 1024 * 1024


def is_direct_audio(url: str) -> bool:
    u = urlparse(url)
    host = u.netloc.lower()
    return u.path.lower().endswith(AUDIO_EXT) and not any(x in host for x in STREAMING)


def budget_state(conn) -> Tuple[float, float]:
    from . import jobs
    spend = jobs.get_control(conn, "openai_audio_spend_usd", 0) or 0
    budget = jobs.get_control(conn, "openai_audio_budget_usd", 25) or 25
    return float(spend), float(budget)


def budget_allows(spend: float, budget: float, minutes: float) -> bool:
    """True when spending for `minutes` more audio keeps the estimated total within budget."""
    return spend < budget and spend + minutes * AUDIO_USD_PER_MIN <= budget + 1e-9


def add_spend(conn, minutes: float) -> float:
    from . import jobs
    spend, _ = budget_state(conn)
    spend = round(spend + minutes * AUDIO_USD_PER_MIN, 4)
    jobs.set_control(conn, "openai_audio_spend_usd", spend, "estimated at $0.006 per audio minute")
    return spend


def audio_minutes(data: bytes, ext: str) -> float:
    import shutil, subprocess, tempfile, os
    if shutil.which("ffprobe"):
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(data)
            name = f.name
        try:
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", name],
                               capture_output=True, text=True, timeout=60)
            return float(r.stdout.strip()) / 60
        except Exception:
            pass
        finally:
            os.unlink(name)
    return len(data) / 16000 / 60          # ~128 kbps


def split_audio(data: bytes, ext: str) -> Optional[List[Tuple[bytes, str, float]]]:
    """[(bytes, filename, minutes)]; chunks of <=20 minutes when over 24 MB (needs ffmpeg, else None)."""
    import glob, os, shutil, subprocess, tempfile
    total = audio_minutes(data, ext)
    if len(data) <= MAX_AUDIO_BYTES:
        return [(data, "audio" + ext, total)]
    if not shutil.which("ffmpeg"):
        return None
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in" + ext)
        open(src, "wb").write(data)
        subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-vn", "-ac", "1", "-b:a", "64k", "-f", "segment",
                        "-segment_time", str(CHUNK_SECONDS), os.path.join(d, "c%03d.mp3")], check=True, timeout=1800)
        out = []
        for f in sorted(glob.glob(os.path.join(d, "c*.mp3"))):
            b = open(f, "rb").read()
            out.append((b, os.path.basename(f), min(CHUNK_SECONDS / 60, total)))
        return out


def pseudo_pages(text: str, size: int = 3000) -> List[str]:
    pages, cur = [], ""
    for w in text.split():
        if len(cur) + len(w) + 1 > size and cur:
            pages.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        pages.append(cur)
    return pages


def transcribe_chunk(data: bytes, name: str, key: str) -> str:
    for model in ("gpt-4o-transcribe", "whisper-1"):
        r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                          headers={"Authorization": f"Bearer {key}"}, data={"model": model},
                          files={"file": (name, data)}, timeout=(15, 600))
        if r.status_code == 200:
            return r.json().get("text", "")
        if r.status_code in (400, 404) and model != "whisper-1":
            continue
        r.raise_for_status()
    raise RuntimeError("transcription failed")


def audio_fallback(db, po: Polite, company_id: int, symbol: str, label, doc_date, log_: list):
    """Transcribe the concall recording that belongs to this transcript. Returns (pages, minutes) or None."""
    import os
    from . import documents as D
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log_.append({"source": "audio", "url": None, "result": "audio_skipped_no_key"})
        return None
    with db.trail.cursor() as cur:
        cur.execute("select url, period_label from equity.document_link where company_id=%s and doc_type='concall_recording'",
                    (company_id,))
        recs = [(u, l) for u, l in cur.fetchall() if l == label or (doc_date and D.derive_doc_date(l) == doc_date)]
    recs = [u for u, _ in recs if is_direct_audio(u)]
    if not recs:
        log_.append({"source": "audio", "url": None, "result": "no direct audio link (mp3/m4a/wav/mp4) for this call"})
        return None
    url = recs[0]
    spend, budget = budget_state(db.trail)
    if spend >= budget:
        log_.append({"source": "audio", "url": url, "result": f"budget exhausted ({spend:.2f}/{budget:.2f} USD)"})
        return None
    try:
        st, data, _ = po.get(url, timeout=(15, 300), max_bytes=500 * 1024 * 1024)
        if st != 200 or not data:
            log_.append({"source": "audio", "url": url, "result": f"download HTTP {st}"})
            return None
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        chunks = split_audio(data, ext)
        if chunks is None:
            log_.append({"source": "audio", "url": url, "result": "audio_skipped_no_ffmpeg (file over 24 MB)"})
            return None
        total_min = sum(m for _, _, m in chunks)
        if not budget_allows(spend, budget, total_min):
            log_.append({"source": "audio", "url": url,
                         "result": f"budget guard: {total_min:.0f} min would exceed {budget:.2f} USD (spent {spend:.2f})"})
            return None
        texts = []
        for b, name, mins in chunks:
            texts.append(transcribe_chunk(b, name, key))
            add_spend(db.trail, mins)
        log_.append({"source": "audio", "url": url, "result": f"transcribed {total_min:.0f} min in {len(chunks)} chunk(s)"})
        return pseudo_pages(" ".join(texts)), total_min
    except Blocked as e:
        log_.append({"source": "audio", "url": url, "result": f"blocked: {e}"})
    except Exception as e:
        log_.append({"source": "audio", "url": url, "result": f"error {type(e).__name__}: {str(e)[:150]}"})
    return None


# ───────── discover: quarters we have no transcript for ─────────
def discover_company(db, company_id: int, run_id: int, po: Optional[Polite] = None) -> dict:
    from . import documents as D
    po = po or Polite()
    stats = {"expected": 0, "found": 0, "missing": 0, "discovered": 0}
    with db.trail.cursor() as cur:
        cur.execute("select coalesce(nse_symbol, screener_slug, bse_code), website from equity.company where company_id=%s",
                    (company_id,))
        symbol, website = cur.fetchone()
        cur.execute("select doc_date, url from equity.document where company_id=%s and doc_type='concall_transcript'",
                    (company_id,))
        rows = cur.fetchall()
        cur.execute("select url from equity.document_link where company_id=%s and doc_type='concall_transcript'", (company_id,))
        known = {u for (u,) in cur.fetchall()} | {u for _, u in rows}
        cur.execute("select doc_date from equity.document where company_id=%s and doc_type='concall_transcript' "
                    "and status='ok' and doc_date is not null", (company_id,))
        have = {reported_quarter_end(d) for (d,) in cur.fetchall()}
    exp, fnd, missing = completeness(expected_quarters(), have)
    stats.update(expected=exp, found=fnd, missing=len(missing))
    if not missing:
        return stats
    notes: list = []
    cands = cached_crawl(po, website, website or "", notes)
    used: set = set()
    for qend in missing:
        best = None
        for url, ctx in cands:
            if url in known or url in used:
                continue
            sc = quarter_score(qend, f"{urlparse(url).path} {ctx}")
            if sc >= 3 and (best is None or sc > best[1]):
                best = (url, sc)
        if best is None:
            continue
        url = best[0]
        q, fy = fiscal_quarter(qend)
        label = f"Q{q} FY{str(fy)[2:]} (discovered)"
        ddate = qend + timedelta(days=1)           # first day after the quarter end: reports as that quarter
        try:
            st, data, _ = po.get(url)
            if st != 200:
                continue
            r = D.process_bytes(data, url, symbol, "concall_transcript", label)
        except Exception as e:
            log.info("%s discover %s: %s", symbol, url, e)
            continue
        if r["status"] != "ok":
            continue
        used.add(url)
        entry = json.dumps([{"source": "company_site", "url": url, "result": f"discovered for {qend} score {best[1]}", "at": _now()}])
        with db.trail.cursor() as cur:
            cur.execute("""insert into equity.document_link (company_id, doc_type, url, title, period_label, source)
                           values (%s,'concall_transcript',%s,%s,%s,'other') on conflict do nothing""",
                        (company_id, url, label, label))
            cur.execute("""insert into equity.document (company_id, doc_type, url, period_label, fiscal_year, doc_date, source)
                           values (%s,'concall_transcript',%s,%s,%s,%s,'other')
                           on conflict (company_id, doc_type, url) do update set period_label=excluded.period_label,
                             fiscal_year=excluded.fiscal_year, doc_date=excluded.doc_date""",
                        (company_id, url, label, fy, ddate))
            D._set_status(cur, company_id, "concall_transcript", url, "ok", **{k: v for k, v in r.items() if k != "status"})
            cur.execute("update equity.document set fetch_source='company_site', fetched_url=%s, attempts_log=%s::jsonb "
                        "where company_id=%s and doc_type='concall_transcript' and url=%s", (url, entry, company_id, url))
        stats["discovered"] += 1
        log.info("%s: discovered transcript for %s at %s", symbol, qend, url)
    return stats
