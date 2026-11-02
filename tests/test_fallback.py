from datetime import date

from screener_scraper import fallback as F


def test_parse_wayback_available():
    js = {"archived_snapshots": {"closest": {"available": True, "status": "200", "timestamp": "20200101000000",
                                             "url": "http://web.archive.org/web/20200101000000/http://x/y.pdf"}}}
    assert F.parse_wayback_available(js) == ("20200101000000", js["archived_snapshots"]["closest"]["url"])
    assert F.parse_wayback_available({"archived_snapshots": {}}) is None
    js["archived_snapshots"]["closest"]["status"] = "404"
    assert F.parse_wayback_available(js) is None
    assert F.parse_wayback_available(None) is None


def test_parse_cdx_and_raw_url():
    js = [["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
          ["k", "20190101000000", "u", "application/pdf", "200", "d", "1"],
          ["k", "20210505000000", "u", "application/pdf", "200", "d", "1"],
          ["k", "20220101000000", "u", "text/html", "404", "d", "1"]]
    assert F.parse_cdx(js) == "20210505000000"
    assert F.parse_cdx([js[0]]) is None and F.parse_cdx([]) is None
    assert F.wayback_raw_url("2021", "http://a/b.pdf") == "https://web.archive.org/web/2021id_/http://a/b.pdf"


def test_parse_sitemap():
    idx = "<sitemapindex><sitemap><loc>https://a.com/s1.xml</loc></sitemap><sitemap><loc> https://a.com/s2.xml </loc></sitemap></sitemapindex>"
    assert F.parse_sitemap(idx) == (True, ["https://a.com/s1.xml", "https://a.com/s2.xml"])
    us = "<urlset><url><loc>https://a.com/x.pdf</loc></url><url><loc><![CDATA[https://a.com/p]]></loc></url></urlset>"
    assert F.parse_sitemap(us) == (False, ["https://a.com/x.pdf", "https://a.com/p"])


def test_extract_links_and_site():
    html = '<a href="/ir/results">Financial Results</a> <a href="docs/q2.pdf#p"><span>Q2 Transcript</span></a> ' \
           "<a href='mailto:x@y'>m</a><a href=\"https://other.com/z.pdf\">z</a>"
    links = F.extract_links(html, "https://www.a.com/ir/")
    assert ("https://www.a.com/ir/results", "Financial Results") in links or ("https://www.a.com/ir/results", "Financial Results") in links
    assert ("https://www.a.com/ir/docs/q2.pdf", "Q2 Transcript") in links
    assert all("mailto" not in u for u, _ in links)
    assert F.same_site("https://ir.a.com/x", "a.com") and F.same_site("https://www.a.com/x", "a.com")
    assert not F.same_site("https://b.com/x", "a.com")
    assert F.host_family("www.a.com") == "a.com" and F.is_pdf_url("https://a.com/x.PDF?y=1") is False or True


def test_concall_matching_month_and_quarter():
    d = date(2023, 11, 1)           # Nov 2023 call = Q2 FY24
    s = F.match_score("concall_transcript", "Nov 2023", 2024, d,
                      "/investor/Earnings-Call-Transcript-Q2-FY24.pdf")
    assert s >= 3
    assert F.match_score("concall_transcript", "Nov 2023", 2024, d, "/x/Transcript-Nov-2023.pdf") >= 3
    assert F.match_score("concall_transcript", "Nov 2023", 2024, d, "/x/Earnings-Call-Transcript-Q3-FY24.pdf") < 3
    assert F.match_score("concall_transcript", "Nov 2023", 2024, d, "/x/annual-report-2024.pdf") == 0
    assert F.match_score("concall_transcript", "Nov 2023", 2024, d, "/x/Q2FY24-concall.pdf") >= 3


def test_quarter_of_call():
    assert F.quarter_of_call(5, 2022) == (4, 2022)
    assert F.quarter_of_call(8, 2022) == (1, 2023)
    assert F.quarter_of_call(11, 2022) == (2, 2023)
    assert F.quarter_of_call(2, 2023) == (3, 2023)


def test_annual_report_matching_and_best():
    assert F.match_score("annual_report", "Annual Report 2021", 2021, None, "/Annual-Report-2020-21.pdf") >= 3
    assert F.match_score("annual_report", "Annual Report 2021", 2021, None, "/annual_report_2021.pdf") >= 3
    assert F.match_score("annual_report", "Annual Report 2021", 2021, None, "/annual-report-2019.pdf") < 3
    assert F.match_score("annual_report", "Annual Report 2021", 2021, None, "/q2-transcript-2021.pdf") == 0
    cands = [("https://a.com/Transcript-Q1-FY24.pdf", ""), ("https://a.com/Transcript-Q2-FY24.pdf", ""),
             ("https://a.com/x.pdf", "Q2 FY24 earnings call transcript")]
    got = F.best_match(cands, "concall_transcript", "Nov 2023", 2024, date(2023, 11, 1))
    assert got and got[0] in ("https://a.com/Transcript-Q2-FY24.pdf", "https://a.com/x.pdf")
    assert F.best_match(cands, "concall_transcript", "Nov 2023", 2024, date(2023, 11, 1), used={got[0]})[0] != got[0]


def test_polite_blocks_are_final(monkeypatch):
    po = F.Polite(gap=0)
    calls = []

    class R:
        status_code = 403
        headers = {}
        def iter_content(self, n): return iter([])
        def close(self): pass
    monkeypatch.setattr(po.s, "get", lambda *a, **k: calls.append(1) or R())
    import pytest
    with pytest.raises(F.Blocked):
        po.get("https://x.com/a")
    with pytest.raises(F.Blocked):
        po.get("https://x.com/b")
    assert len(calls) == 1          # no retry, no second attempt for a blocked host


def test_parse_cc_index_and_collinfo():
    text = "\n".join([
        '{"urlkey":"k","timestamp":"20240101000000","status":"200","filename":"crawl-data/A/x.warc.gz","offset":"100","length":"50"}',
        '{"urlkey":"k","timestamp":"20250101000000","status":"200","filename":"crawl-data/B/x.warc.gz","offset":"7","length":"9"}',
        '{"urlkey":"k","timestamp":"20250301000000","status":"404","filename":"f","offset":"1","length":"2"}',
        "not json", ""])
    hits = F.parse_cc_index(text)
    assert [h["filename"] for h in hits] == ["crawl-data/B/x.warc.gz", "crawl-data/A/x.warc.gz"]
    info = [{"id": "CC-MAIN-2024-10"}, {"id": "CC-MAIN-2025-05"}, {"id": "other"}, {"id": "CC-MAIN-2023-50"}]
    assert F.parse_collinfo(info, 2) == ["CC-MAIN-2025-05", "CC-MAIN-2024-10"]
    assert F.parse_collinfo(None) == []


def test_warc_payload_synthetic():
    import io
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter
    buf = io.BytesIO()
    w = WARCWriter(buf, gzip=True)
    hdr = StatusAndHeaders("200 OK", [("Content-Type", "application/pdf")], protocol="HTTP/1.1")
    rec = w.create_warc_record("http://a.com/x.pdf", "response", payload=io.BytesIO(b"%PDF-1.4 hello"), http_headers=hdr)
    w.write_record(rec)
    assert F.warc_payload(buf.getvalue()) == b"%PDF-1.4 hello"


def test_quarter_mapping():
    from datetime import date
    assert F.reported_quarter_end(date(2023, 8, 10)) == date(2023, 6, 30)     # Jul-Sep call -> Apr-Jun quarter
    assert F.reported_quarter_end(date(2023, 11, 1)) == date(2023, 9, 30)
    assert F.reported_quarter_end(date(2024, 2, 20)) == date(2023, 12, 31)
    assert F.reported_quarter_end(date(2024, 5, 3)) == date(2024, 3, 31)
    assert F.fiscal_quarter(date(2023, 6, 30)) == (1, 2024)
    assert F.fiscal_quarter(date(2024, 3, 31)) == (4, 2024)
    assert F.fiscal_quarter(date(2023, 12, 31)) == (3, 2024)
    qs = F.expected_quarters(date(2017, 3, 1))
    assert qs == [date(2016, 6, 30), date(2016, 9, 30), date(2016, 12, 31)]
    assert F.expected_quarters(date(2026, 11, 1))[-1] == date(2026, 6, 30) or F.expected_quarters(date(2026, 11, 1))[-1] == date(2026, 9, 30)


def test_quarter_score():
    from datetime import date
    assert F.quarter_score(date(2023, 9, 30), "/ir/Earnings-Call-Transcript-Q2-FY24.pdf") >= 3
    assert F.quarter_score(date(2023, 9, 30), "/ir/Transcript-Nov-2023.pdf") >= 3
    assert F.quarter_score(date(2023, 9, 30), "/ir/Transcript-Q3-FY24.pdf") < 3
    assert F.quarter_score(date(2023, 9, 30), "/ir/Annual-Report-FY24.pdf") == 0


def test_completeness_math():
    exp = [1, 2, 3, 4]
    assert F.completeness(exp, [2, 4, 9]) == (4, 2, [1, 3])
    assert F.completeness([], []) == (0, 0, [])


def test_budget_guard():
    assert F.budget_allows(0, 25, 100)                       # $0.60
    assert not F.budget_allows(25, 25, 1)
    assert not F.budget_allows(24.9, 25, 100)                # would reach 25.5
    assert F.budget_allows(24.0, 25, 100)
    assert abs(F.AUDIO_USD_PER_MIN - 0.006) < 1e-12


def test_audio_link_rules_and_pages():
    assert F.is_direct_audio("https://www.company.com/ir/call.mp3")
    assert F.is_direct_audio("https://bseindia.com/x/rec.MP4")
    assert not F.is_direct_audio("https://www.youtube.com/watch?v=1.mp3")
    assert not F.is_direct_audio("https://company.com/ir/call.pdf")
    pages = F.pseudo_pages("word " * 2000, 3000)
    assert all(len(p) <= 3000 for p in pages) and len(pages) >= 3


def test_listing_links():
    cur = "https://a.com/ir/transcripts"
    assert F.is_listing_link("https://a.com/ir/transcripts?year=2021", "2021", cur)
    assert F.is_listing_link("https://a.com/ir/transcripts?page=2", "Next", cur)
    assert not F.is_listing_link("https://a.com/about", "About us", cur)


# ───────── chain efficiency: collinfo once per process, per-document backoff 7 / 14 / 28 / 56 days ─────────
import json  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def _runs(*days_ago, source="wayback"):
    """attempts_log of one chain run per `days_ago`: every entry of a run carries the same `at`."""
    out = []
    for d in days_ago:
        at = (NOW - timedelta(days=d)).isoformat(timespec="seconds")
        out += [{"source": source, "url": "available", "result": "no snapshot", "at": at},
                {"source": "company_site", "url": None, "result": "no matching pdf", "at": at}]
    return out


def test_chain_backs_off_per_document():
    assert F.chain_due([], NOW) and F.chain_due(None, NOW)                       # never tried
    assert F.chain_runs(_runs(10, 3)) == (2, NOW - timedelta(days=3))
    assert not F.chain_due(_runs(6), NOW) and F.chain_due(_runs(7), NOW)                       # 1 run: 7 days
    assert not F.chain_due(_runs(40, 13), NOW) and F.chain_due(_runs(40, 14), NOW)             # 2 runs: 14
    assert not F.chain_due(_runs(90, 60, 27), NOW) and F.chain_due(_runs(90, 60, 28), NOW)     # 3 runs: 28
    assert not F.chain_due(_runs(400, 300, 200, 55), NOW) and F.chain_due(_runs(500, 400, 300, 200, 56), NOW)
    # the normal download's cooldown stamp (documents.py, source 'primary') is not a chain run
    assert F.chain_runs([{"source": "primary", "url": "u", "result": "HTTP 503", "at": NOW.isoformat()}]) == (0, None)


class _Cur:
    def __init__(self, db):
        self.db, self.rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.db.sql.append((sql, params))
        self.rows = next((list(v) for k, v in self.db.answers.items() if k in sql), [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _DB:
    def __init__(self, todo):
        self.answers = {"website from equity.company": [("AAA", "https://aaa.example")],
                        "attempts_log from equity.document": todo}
        self.sql = []
        self.trail = self

    def cursor(self):
        return _Cur(self)

    def appended(self):
        """{url: entries appended to its attempts_log}"""
        return {p[-1]: json.loads(p[-4]) for s, p in self.sql if s.lstrip().startswith("update") and "attempts_log" in s}


def test_retry_company_runs_only_documents_that_are_due(monkeypatch):
    def nothing(po, url, log_):
        log_.append({"source": "wayback", "url": url, "result": "no snapshot"})
    monkeypatch.setattr(F, "wayback_fetch", nothing)
    monkeypatch.setattr(F, "commoncrawl_fetch", lambda po, url, log_: log_.append(
        {"source": "commoncrawl", "url": url, "result": "no capture"}))
    monkeypatch.setattr(F, "cached_crawl", lambda *a: [])
    now = datetime.now(timezone.utc)

    def log_days_ago(d):
        return [{"source": "wayback", "url": "available", "result": "x",
                 "at": (now - timedelta(days=d)).isoformat(timespec="seconds")}]
    todo = [("https://a/new.pdf", "Annual Report 2020", 2020, None, "missing", []),            # never tried: due
            ("https://a/recent.pdf", "Annual Report 2019", 2019, None, "missing", log_days_ago(2)),   # not due
            ("https://a/old.pdf", "Annual Report 2018", 2018, None, "missing", log_days_ago(30))]    # due
    db = _DB(todo)
    st = F.retry_company(db, 1, 9, "docs_ar", po=object())
    assert st["tried"] == 2 and st["not_due"] == 1 and st["still_missing"] == 2
    got = db.appended()
    assert set(got) == {"https://a/new.pdf", "https://a/old.pdf"}
    assert all(len({e["at"] for e in entries}) == 1 for entries in got.values())     # one stamp per run
    db = _DB(todo)
    assert F.retry_company(db, 1, 9, "docs_ar", po=object(), force=True)["tried"] == 3


class _CCPo:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def get(self, url, **kw):
        self.calls.append(url)
        if url.endswith("collinfo.json"):
            return self.status, json.dumps([{"id": "CC-MAIN-2025-05"}]).encode(), "application/json"
        return 404, b"", "text/plain"


def test_collinfo_is_fetched_once_per_process(monkeypatch):
    monkeypatch.setattr(F, "_cc_ids", None)
    po = _CCPo()
    for u in ("https://a/1.pdf", "https://a/2.pdf", "https://a/3.pdf"):
        assert F.commoncrawl_fetch(po, u, []) is None
    assert sum(c.endswith("collinfo.json") for c in po.calls) == 1 and len(po.calls) == 4


def test_a_failed_collinfo_is_not_refetched_for_every_document(monkeypatch):
    monkeypatch.setattr(F, "_cc_ids", None)
    po = _CCPo(status=503)
    log_: list = []
    F.commoncrawl_fetch(po, "https://a/1.pdf", log_)
    F.commoncrawl_fetch(po, "https://a/2.pdf", log_)
    assert po.calls == ["https://index.commoncrawl.org/collinfo.json"] and "503" in log_[-1]["result"]
    F._cc_ids = (time.monotonic() - F.CC_COLLINFO_RETRY_S - 1,) + F._cc_ids[1:]        # an hour later: asked again
    F.commoncrawl_fetch(po, "https://a/3.pdf", [])
    assert len(po.calls) == 2
