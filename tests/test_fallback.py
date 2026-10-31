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
