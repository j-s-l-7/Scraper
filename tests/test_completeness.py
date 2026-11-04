import unittest
from datetime import date

from screener_scraper import completeness as C
from screener_scraper.sources import nse_pr

TODAY = date(2026, 11, 4)
CAL_LAST = date(2026, 11, 3)


def co(listed=None, last=CAL_LAST, sme=False, fy=3):
    return C.Co(1, "X", "INE000000000", None, 1, listed, last, sme, fy)


class Calendar(unittest.TestCase):
    def test_months_and_quarters(self):
        self.assertEqual(C.add_months(date(2026, 3, 31), 6), date(2026, 9, 30))
        self.assertEqual(C.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(C.add_months(date(2026, 3, 15), -12), date(2025, 3, 15))
        self.assertEqual(C.quarters_back(date(2026, 6, 30), 3), [date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)])
        self.assertEqual(C.quarters_between(date(2025, 11, 2), date(2026, 3, 31)), [date(2025, 12, 31), date(2026, 3, 31)])
        self.assertEqual(C.quarters_between(date(2026, 8, 1), date(2026, 6, 30)), [])
        self.assertEqual(C.reported_quarter(date(2026, 10, 20)), date(2026, 9, 30))
        self.assertEqual(C.reported_quarter(date(2026, 5, 2)), date(2026, 3, 31))
        self.assertEqual(C.fy_of(date(2026, 3, 31)), 2026)
        self.assertEqual(C.fy_of(date(2025, 6, 30)), 2026)
        self.assertEqual(C.fy_of(date(2025, 12, 31), 12), 2025)

    def test_due_dates(self):
        # Sep-2026 results are due 14 Nov: on 4 Nov the last due quarter is Jun 2026
        self.assertEqual(C.last_due_quarter(TODAY, 3), date(2026, 6, 30))
        self.assertEqual(C.last_due_quarter(date(2026, 11, 15), 3), date(2026, 9, 30))
        self.assertEqual(C.shp_periods(TODAY)[-1], date(2026, 9, 30))       # SHP due 21 Oct
        self.assertEqual(C.fin_year_periods(co(), TODAY)[-1], date(2026, 3, 31))
        # an early filer's window has moved on: anchored at its newest published quarter
        self.assertEqual(C.fin_quarter_periods(co(), TODAY, date(2026, 9, 30))[0], date(2023, 9, 30))

    def test_first_annual_report(self):
        self.assertEqual(C.first_ar_fy(None), C.AR_FIRST_FY)
        self.assertEqual(C.first_ar_fy(date(2026, 10, 28)), 2027)      # listed after the FY26 AGM deadline
        self.assertEqual(C.first_ar_fy(date(2025, 8, 1)), 2025)        # listed before the FY25 AGM: files FY25
        self.assertEqual(C.first_ar_fy(date(2025, 11, 1)), 2026)

    def test_listing_date(self):
        L, d = C.listing_date, date
        self.assertIsNone(L(d(2010, 1, 4), d(2016, 12, 8)))                 # in both archives' first files
        self.assertEqual(L(d(2015, 11, 10), d(2016, 12, 8)), d(2015, 11, 10))  # IndiGo: NSE IPO before BSE's archive
        self.assertIsNone(L(d(2026, 10, 30), d(2016, 12, 8)))               # INDTONER: old BSE company, NSE in 2026
        self.assertEqual(L(d(2026, 10, 28), d(2026, 10, 28)), d(2026, 10, 28))   # Jio Platforms
        self.assertEqual(L(None, d(2019, 3, 1)), d(2019, 3, 1))
        self.assertIsNone(L(None, None))

    def test_sessions_by_year(self):
        cal = [date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 5)]
        self.assertEqual(C.sessions_by_year(cal, date(2025, 12, 31), date(2026, 1, 4)), {2025: 1, 2026: 1})


class Expectations(unittest.TestCase):
    def test_recent_ipo_explains_empty_datasets(self):
        jio = co(listed=date(2026, 10, 28))                           # Jio Platforms
        ar = C.expect_annual_reports(jio, {}, TODAY, CAL_LAST)
        self.assertEqual((ar.expected, ar.gaps), (0, []))
        self.assertEqual(ar.note.reason, "listed_recently")
        self.assertEqual(ar.note.detail["first_report_due"], "2027-10-31")
        cc = C.expect_concalls(jio, {}, {}, TODAY, CAL_LAST)
        self.assertEqual(cc.note.reason, "listed_recently")
        fq = C.expect_periods("fin_quarterly", jio, C.fin_quarter_periods(jio, TODAY), {}, lambda q: 45, TODAY, CAL_LAST)
        self.assertEqual(fq.expected, 0)
        self.assertEqual(fq.note.reason, "listed_recently")

    def test_annual_reports_reasons(self):
        docs = {2024: {"status": "ok", "runs": 0, "tried": []},
                2025: {"status": "missing", "runs": 4, "tried": ["wayback", "company_site"]},
                2026: {"status": "pending", "runs": 0, "tried": []}}
        r = C.expect_annual_reports(co(listed=date(2023, 1, 10)), docs, TODAY, CAL_LAST)
        self.assertEqual((r.expected, r.present), (4, 1))                # FY2023..FY2026
        by = {g.period: g for g in r.gaps}
        self.assertEqual(by["FY2023"].reason, "no_link")
        self.assertEqual(by["FY2025"].reason, "exhausted")
        self.assertEqual(by["FY2026"].reason, "queued")
        self.assertEqual(by["FY2026"].state, "open")

    def test_moved_from_bse_is_long_listed(self):
        # INDTONER: on BSE before the BSE archive starts, on NSE only from Oct 2026 -> long listed, reports expected
        r = C.expect_annual_reports(co(listed=None), {}, TODAY, CAL_LAST)
        self.assertEqual(r.expected, 2026 - C.AR_FIRST_FY + 1)

    def test_doc_chain_next_source(self):
        g = C.doc_gap("concall_transcript", "2025-06-30", "missing", 1, ["original_retry", "wayback"])
        self.assertEqual((g.reason, g.next_source), ("dead_link", "commoncrawl"))
        self.assertEqual(C.doc_gap("annual_report", "FY2020", "no_text", 0, []).next_source, "ocr")

    def test_concalls_from_first_call(self):
        tr = {date(2026, 3, 31): {"status": "ok", "runs": 0, "tried": []}}
        ev = {date(2025, 6, 30): {"concall_recording"}, date(2025, 9, 30): {"concall_ppt"},
              date(2026, 3, 31): {"concall_transcript", "concall_ppt"}, date(2026, 6, 30): {"concall_transcript"}}
        r = C.expect_concalls(co(), tr, ev, TODAY, CAL_LAST)
        by = {g.period: g.reason for g in r.gaps}
        self.assertEqual((r.expected, r.present), (5, 1))             # Jun-25 .. Jun-26
        self.assertEqual(by["2025-06-30"], "call_held_no_transcript")  # a recording proves a call
        self.assertEqual(by["2025-09-30"], "deck_only")                # a deck alone does not
        self.assertEqual(by["2025-12-31"], "no_call_that_quarter")
        self.assertEqual(by["2026-06-30"], "queued")                   # linked, not downloaded yet
        self.assertEqual(C.expect_concalls(co(), {}, {}, TODAY, CAL_LAST).note.reason, "no_concalls")
        ok = {"status": "ok", "runs": 0, "tried": []}
        reg = {date(2025, 9, 30): ok, date(2026, 3, 31): ok}           # Dec-25 between two transcripts
        r = C.expect_concalls(co(), reg, {date(2025, 12, 31): {"concall_ppt"}}, TODAY, CAL_LAST)
        g = {g.period: g for g in r.gaps}["2025-12-31"]
        self.assertEqual((g.reason, g.next_source), ("call_likely_missed", "company_site"))

    def test_statement_windows(self):
        hcl = co(fy=3)
        yrs = C.fin_year_periods(hcl, TODAY)                          # Mar 2015 .. Mar 2026
        have = {date(2015, 6, 30): "screener", **{date(y, 3, 31): "screener" for y in range(2017, 2027)}}
        r = C.expect_periods("fin_annual", hcl, yrs, have, lambda q: 60, TODAY, CAL_LAST, by_fy=True)
        self.assertEqual((r.expected, r.present), (12, 11))           # Jun 2015 counts for FY2016
        self.assertEqual([(g.period, g.reason) for g in r.gaps], [("2015-03-31", "before_source_history")])
        qs = C.fin_quarter_periods(co(), TODAY)
        hole = {q: "screener" for q in qs if q != qs[5]}
        r = C.expect_periods("fin_quarterly", co(), qs, hole, lambda q: 45, TODAY, CAL_LAST)
        self.assertEqual([g.reason for g in r.gaps], ["missing_in_source"])   # a hole inside the series is open

    def test_sme_half_yearly(self):
        qs = C.fin_quarter_periods(co(sme=True), TODAY)
        self.assertTrue(qs and all(q.month in (3, 9) for q in qs))

    def test_suspended(self):
        s = co(last=date(2024, 5, 10))
        r = C.expect_periods("shareholding", s, C.shp_periods(TODAY), {}, lambda q: 21, TODAY, CAL_LAST)
        by = {g.period: g.reason for g in r.gaps}
        self.assertEqual(by["2026-09-30"], "suspended")                # after the last trade
        self.assertEqual(by["2024-03-31"], "missing_in_source")        # while it still traded
        self.assertEqual(C.parse_purpose("DIV 2018-19 RS 3 + BONUS 1:1"), [("bonus", 2.0), ("dividend", 3.0)])
        p = C.expect_prices(s, [date(2024, 5, 9), date(2024, 5, 10), date(2024, 5, 13)], {2024: 1}, CAL_LAST)
        moved = C.Co(2, "Y", None, "500001", 9, None, CAL_LAST, nse_listed=date(2026, 10, 30), nse_last=CAL_LAST)
        self.assertEqual(C.expect_prices(moved, [date(2026, 10, 29), date(2026, 10, 30), CAL_LAST], {2026: 2},
                                         CAL_LAST).expected, 2)          # NSE series starts at the NSE listing
        self.assertEqual((p.expected, p.present, p.counts()[2]), (2, 1, 1))
        self.assertEqual(p.note.reason, "suspended")
        q = C.expect_prices(co(), [date(2026, 11, 2), CAL_LAST], {2026: 0}, CAL_LAST, {2026: 1})
        self.assertEqual(sorted((g.period, g.reason, q.weight[g.period]) for g in q.gaps),
                         [("2026", "no_trade", 1), ("2026:basis", "basis_conflict", 1)])

    def test_dividends(self):
        pay = {date(2024, 3, 31): 30.0, date(2025, 3, 31): 0.0, date(2026, 3, 31): 25.0}
        r = C.expect_dividends(co(), pay, [date(2024, 8, 1)], (date(2010, 1, 4), date(2026, 12, 31)))
        self.assertEqual((r.expected, r.present), (2, 1))
        self.assertEqual(r.gaps[0].reason, "exhausted")              # yfinance and the NSE archive both read
        r = C.expect_dividends(co(), pay, [], (date(2010, 1, 4), date(2015, 6, 30)))   # backfill midway
        self.assertEqual({g.reason for g in r.gaps}, {"no_record"})
        self.assertEqual(C.expect_dividends(co(), {date(2025, 3, 31): 0.0}, [], None).note.reason, "no_dividends")
        moved = C.Co(3, "Z", None, "500002", 9, None, CAL_LAST, nse_listed=date(2018, 1, 10), nse_last=CAL_LAST)
        g = C.expect_dividends(moved, {date(2016, 3, 31): 20.0}, [], (date(2010, 1, 4), date(2026, 12, 31))).gaps[0]
        self.assertEqual((g.reason, g.next_source), ("no_record", "yfinance_bse"))   # BSE-only years: BSE ticker


class Purposes(unittest.TestCase):
    def test_parse(self):
        P = C.parse_purpose
        self.assertEqual(P("INTDIV - RS 4.30 PER SH"), [("dividend", 4.3)])
        self.assertEqual(P("DIVIDEND-RE.1/- PER SHARE"), [("dividend", 1.0)])
        self.assertEqual(P("AGM/DIV-SPL 10+FIN 1 P S"), [("dividend", 11.0)])
        self.assertEqual(P("DIVIDEND - 50%", face_value=2.0), [("dividend", 1.0)])
        self.assertEqual(P("INTERIM DIVIDEND"), [("dividend", None)])
        self.assertEqual(P("DIVIDEND (2ND INTERIM)"), [("dividend", None)])
        self.assertEqual(P("DIV-RS.10 PER SHARE 2023"), [("dividend", 10.0)])
        self.assertEqual(P("INT DIV 6.5 PER SHARE"), [("dividend", 6.5)])
        self.assertEqual(P("INT DIV-RS3/SPLT-RS5TORS2"), [("split", 2.5), ("dividend", 3.0)])
        self.assertEqual(P("AGM/DIV FIN RS 13+SPLRS20"), [("dividend", 33.0)])
        # forms the GitHub survey found in the archive
        self.assertEqual(P("AGM/DIVRE 0.50 PER SHARE"), [("dividend", 0.5)])
        self.assertEqual(P("INT DIVD RS.5/- PER SHARE"), [("dividend", 5.0)])
        self.assertEqual(P("DIVINDEND-RE 1 PER SHARE"), [("dividend", 1.0)])
        self.assertEqual(P("DIVD RS. 6/-"), [("dividend", 6.0)])
        self.assertEqual(P("INT RS 5/-"), [("dividend", 5.0)])
        self.assertEqual(P("FVSPLTFRM10TO1"), [("split", 10.0)])
        self.assertEqual(P("FV SPL-RS10TORS5/BON-2:1"), [("bonus", 3.0), ("split", 2.0)])   # 2 new per 1 held
        self.assertEqual(P("BON 1:1/FV SPL RS.5TORE.1"), [("bonus", 2.0), ("split", 5.0)])
        self.assertEqual(P("BONUS @ 1:1"), [("bonus", 2.0)])
        self.assertEqual(P("BONUS: 1:4"), [("bonus", 1.25)])
        self.assertEqual(P("BONUS 1.5:1"), [("bonus", 2.5)])
        self.assertEqual(P("RIGHTS@3:2 AT PAR"), [("rights", 1.5)])
        self.assertEqual(P("RIGHTS: 4CCD:5EQ"), [("rights", 0.8)])
        self.assertEqual(P("CONSOLIDATION RE1 TO RS10"), [("split", 0.1)])
        self.assertEqual(P("FINAL DIVIDEND RS 2 PER SHARE (FACE VALUE RS 1)"), [("dividend", 2.0)])
        self.assertEqual(P("SPL DIV RS 5"), [("dividend", 5.0)])                       # SPL alone is not a split
        self.assertEqual(P("SCHEME OF ARRANGEMENT - SUB-DIVISION"), [])
        self.assertEqual(P("CAP RED RS 10 TO RS 2"), [])
        self.assertEqual(P("BON1:1/FVSPLTFRMRS5TORS2"), [("bonus", 2.0), ("split", 2.5)])
        self.assertEqual(P("BONUS2:1/FVSPLIT10TO2"), [("bonus", 3.0), ("split", 5.0)])
        self.assertEqual(P("SUB DIV FRM RS 10 TO RS 2"), [("split", 5.0)])
        for not_equity in ("AGM/DIV-RS3/BON DEB 1:1", "SCH AGMT-BONUS DEBT1:1", "BONUS NCRPS1:116", "BONUS 1 DVR : 10 EQ SHARE"):
            self.assertNotIn("bonus", [k for k, _ in P(not_equity)], not_equity)
        self.assertEqual(P("BOND ISSUE 1:1"), [])
        self.assertEqual(P("BONUS 1:1"), [("bonus", 2.0)])
        self.assertEqual(P("BONUS 1:2"), [("bonus", 1.5)])
        self.assertEqual(P("FACE VALUE SPLIT (SUB-DIVISION) - FROM RS 10/- PER SHARE TO RS 2/- PER SHARE"),
                         [("split", 5.0)])
        self.assertEqual(P("RIGHTS 1:5 @ PREMIUM RS 90/-"), [("rights", 0.2)])
        self.assertEqual(P("ANNUAL GENERAL MEETING"), [])
        self.assertEqual(P("INTEREST PAYMENT"), [])

    def test_pr_files(self):
        bc = ("SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE\n"
              "BE,OIL,Oil India Ltd,04/01/2012, , ,03/01/2012, , ,INT DIV-RS.25/- PER SHARE\n"
              "EQ,OIL,Oil India Ltd,04/01/2012, , ,03/01/2012, , ,INT DIV-RS.25/- PER SHARE\n"
              "N1,1003SCL32,Sammaan,2026-11-04,,,2026-11-04,,,INTEREST PAYMENT\n")
        rows = nse_pr.parse_bc(bc)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:4], ("OIL", date(2012, 1, 3), "INT DIV-RS.25/- PER SHARE", "EQ"))
        mc = ("Trade Date,Symbol,Series,Security Name,Category,Last Trade Date,Face Value(Rs.),Issue Size,"
              "Close Price/Paid up value(Rs.),Market Cap(Rs.)\n"
              "03 NOV 2026,20MICRONS,EQ,20 MICRONS LTD,Listed,03 NOV 2026, 5.00, 35286502, 174.85, 6169139144.65\n"
              "03 NOV 2026,GOI,GS,GOVT,Listed,03 NOV 2026, 100.00, 1, 100, 100\n")
        self.assertEqual(nse_pr.parse_mcap(mc, date(2026, 11, 3)), [("20MICRONS", "EQ", 5.0, 35286502)])


if __name__ == "__main__":
    unittest.main()
