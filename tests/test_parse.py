"""Parser tests against real saved pages (plain asserts; run with `python3 -m unittest` or pytest)."""
import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from screener_scraper import parse  # noqa: E402

FX = ROOT / "tests" / "fixtures"


def load(name):
    return parse.parse_page((FX / f"{name}.html").read_text(encoding="utf-8"))


def row(page, section, label):
    return next(r for r in page.statements[section] if r.label == label)


def val(r, ptype, d):
    return next((v for pt, pe, v in r.values if pt == ptype and pe == d), None)


class Helpers(unittest.TestCase):
    def test_month_end_and_numbers(self):
        assert parse.month_end("Mar 2026") == date(2026, 3, 31)
        assert parse.month_end("Feb 2024") == date(2024, 2, 29)
        assert parse.month_end("TTM") is None
        assert parse.parse_number("1,07,000") == 107000.0
        assert parse.parse_number("-22%") == -22.0
        assert parse.parse_number("₹ 5.5") == 5.5
        assert parse.parse_number("") is None and parse.parse_number("-") is None

    def test_units(self):
        assert parse.unit_for("OPM %", ["15%"]) == "pct"
        assert parse.unit_for("EPS in Rs", ["1"]) == "rs"
        assert parse.unit_for("Debtor Days", ["13"]) == "days"
        assert parse.unit_for("Cash Conversion Cycle", ["-3"]) == "days"
        assert parse.unit_for("Sales", ["1,000"]) == "cr"
        assert parse.unit_for("CFO/EBITDA", ["105%", "99%"]) == "pct"

    def test_series_slug(self):
        assert parse.series_slug("Price to Earning") == "pe"
        assert parse.series_slug("Median PE") == "median_pe"
        assert parse.series_slug("GPM") == "gpm"
        assert parse.series_slug("Some New-Thing") == "some_new_thing"


class Identity(unittest.TestCase):
    def test_reliance(self):
        i = load("rel").info
        assert (i.company_id, i.warehouse_id) == (2726, 6598251)
        assert i.name == "Reliance Industries Ltd"
        assert (i.nse_symbol, i.bse_code) == ("RELIANCE", "500325")
        assert i.website == "http://www.ril.com"
        assert (i.sector, i.industry_group, i.industry, i.sub_industry) == (
            "Energy", "Oil, Gas & Consumable Fuels", "Petroleum Products", "Refineries & Marketing")
        assert i.industry_code == "IN030103001"
        assert i.about.startswith("Reliance was founded") and i.key_points and len(i.cons) == 2

    def test_standalone_page_has_other_warehouse_id(self):
        i = load("rels").info
        assert i.company_id == 2726 and i.warehouse_id == 811

    def test_bank(self):
        i = load("hdfc").info
        assert (i.company_id, i.nse_symbol, i.bse_code, i.industry_code) == (1298, "HDFCBANK", "500180", "IN050102002")
        assert len(i.pros) == 3 and len(i.cons) == 3


class Basis(unittest.TestCase):
    def test_labels(self):
        assert load("rel").basis_label == "C" and load("rel").is_consolidated
        assert load("rels").basis_label == "S" and not load("rels").is_consolidated
        assert load("hdfc").is_consolidated

    def test_no_consolidated_company(self):
        # IRFC: /consolidated/ is an empty shell (no labels, no values, blank top ratios)
        c = load("irfc")
        assert c.basis_label is None and not c.has_financials and not c.is_consolidated
        assert c.top_ratios.market_cap_cr is None
        s = load("irfs")
        assert s.basis_label is None and s.has_financials and s.top_ratios.market_cap_cr == 95884.0
        assert s.info.warehouse_id == 60406451


class Statements(unittest.TestCase):
    def setUp(self):
        self.p = load("rel")

    def test_labels_cleaned(self):
        labels = [r.label for r in self.p.statements["profit-loss"]]
        assert labels[:3] == ["Sales", "Expenses", "Operating Profit"]
        assert "Raw PDF" not in [r.label for r in self.p.statements["quarters"]]
        assert "CFO/EBITDA" in [r.label for r in self.p.statements["cash-flow"]]
        assert not any(l.endswith("+") for sec in self.p.statements.values() for l in (r.label for r in sec))

    def test_units(self):
        assert row(self.p, "profit-loss", "OPM %").unit == "pct"
        assert row(self.p, "profit-loss", "EPS in Rs").unit == "rs"
        assert row(self.p, "ratios", "Debtor Days").unit == "days"
        assert row(self.p, "ratios", "Cash Conversion Cycle").unit == "days"
        assert row(self.p, "cash-flow", "CFO/EBITDA").unit == "pct"
        assert row(self.p, "profit-loss", "Sales").unit == "cr"

    def test_values_and_period_types(self):
        sales = row(self.p, "profit-loss", "Sales")
        assert val(sales, "A", date(2026, 3, 31)) == 1055780.0
        assert val(sales, "TTM", date(2026, 9, 30)) == 1199033.0
        assert val(sales, "A", date(2015, 3, 31)) is not None
        ttm = [v for v in sales.values if v[0] == "TTM"]
        assert len(ttm) == 1 and ttm[0][1] == date(2026, 9, 30) == self.p.ttm_end
        assert row(self.p, "profit-loss", "Tax %").values[-1][0] != "X"
        q = row(self.p, "quarters", "Sales")
        assert all(pt == "Q" for pt, _, _ in q.values) and val(q, "Q", date(2026, 9, 30)) == 330601.0

    def test_empty_cells_skipped(self):
        tax = row(self.p, "profit-loss", "Tax %")        # TTM cell is blank
        assert not [v for v in tax.values if v[0] == "TTM"]

    def test_balance_sheet_half_year_column(self):
        assert self.p.bs_fy_month == 3
        eq = row(self.p, "balance-sheet", "Equity Capital")
        assert val(eq, "A", date(2026, 3, 31)) is not None
        assert val(eq, "H", date(2026, 9, 30)) is not None
        assert not [v for v in eq.values if v[0] == "A" and v[1].month != 3]

    def test_schedule_buttons(self):
        assert ("Sales", "profit-loss") in self.p.schedule_buttons
        assert ("Borrowings", "balance-sheet") in self.p.schedule_buttons
        assert ("Sales", "quarters") in self.p.schedule_buttons
        assert row(self.p, "profit-loss", "Sales").schedule_parent == "Sales"
        assert row(self.p, "profit-loss", "Operating Profit").schedule_parent is None

    def test_standalone_differs(self):
        s = load("rels")
        assert val(row(s, "profit-loss", "Sales"), "A", date(2026, 3, 31)) != 1055780.0


class Bank(unittest.TestCase):
    def test_bank_layout(self):
        p = load("hdfc")
        labels = [r.label for r in p.statements["profit-loss"]]
        assert labels[:5] == ["Revenue", "Interest", "Expenses", "Financing Profit", "Financing Margin %"]
        assert row(p, "profit-loss", "Financing Margin %").unit == "pct"
        bs = [r.label for r in p.statements["balance-sheet"]]
        assert "Deposits" in bs and "Borrowing" in bs
        assert val(row(p, "balance-sheet", "Deposits"), "H", date(2026, 9, 30)) is not None
        assert "Gross NPA %" not in [r.label for r in p.statements["quarters"]]   # all-empty rows are skipped
        assert ("Revenue", "profit-loss") in p.schedule_buttons


class TopRatiosAndGrowth(unittest.TestCase):
    def test_top_ratios(self):
        t = load("rel").top_ratios
        assert (t.market_cap_cr, t.price, t.high_52w, t.low_52w) == (1576405.0, 1165.0, 1612.0, 1152.0)
        assert (t.pe, t.book_value, t.dividend_yield_pct, t.roce_pct, t.roe_pct, t.face_value) == (
            19.2, 698.0, 0.52, 10.3, 8.91, 10.0)
        assert load("rels").top_ratios.pe == 34.7     # standalone differs

    def test_extra_labels(self):
        html = ('<ul id="top-ratios"><li><span class="name">Market Cap</span><span class="value">'
                '<span class="number">1,000</span></span></li><li><span class="name">Gross NPA</span>'
                '<span class="value"><span class="number">1.5</span>%</span></li></ul>')
        t = parse.parse_top_ratios(parse.soup(html))
        assert t.market_cap_cr == 1000.0 and t.extra == {"Gross NPA": 1.5}

    def test_growth(self):
        g = {(x.measure, x.horizon): x.value_pct for x in load("rel").growth}
        assert g[("sales_growth", "10Y")] == 15.0 and g[("sales_growth", "TTM")] == 20.0
        assert g[("profit_growth", "3Y")] == 5.0
        assert g[("price_cagr", "1Y")] == -22.0
        assert g[("roe", "LAST")] == 9.0
        assert len(g) == 16


class Shareholding(unittest.TestCase):
    def test_tables(self):
        p = load("rel")
        q = {(r.category, r.period_end): r.value for r in p.shareholding if r.frequency == "Q"}
        y = {(r.category, r.period_end): r.value for r in p.shareholding if r.frequency == "Y"}
        assert q[("promoters", date(2026, 9, 30))] == 50.44
        assert q[("fii", date(2026, 9, 30))] == 16.67
        assert q[("num_shareholders", date(2026, 9, 30))] == 5072457.0
        assert ("promoters", date(2017, 3, 31)) in y and ("promoters", date(2017, 3, 31)) not in q
        assert {r.category for r in p.shareholding} >= {"promoters", "fii", "dii", "government", "public", "num_shareholders"}

    def test_buttons(self):
        b = load("rel").holder_buttons
        assert b["quarterly"] == ["promoters", "foreign_institutions", "domestic_institutions", "government", "public"]
        assert "promoters" not in load("hdfc").holder_buttons["quarterly"]     # no promoter button for HDFC Bank

    def test_investors_json(self):
        js = json.dumps({"Foo LLP": {"Dec 2023": "11.20", "Mar 2024": "11.10",
                                     "setAttributes": {"data-person-url": "/people/1/foo/"}},
                         "Bar": {"Dec 2023": "1.5"}})
        rows = parse.parse_investors_json(js)
        assert ("Foo LLP", "/people/1/foo/", date(2023, 12, 31), 11.2) in rows
        assert ("Bar", None, date(2023, 12, 31), 1.5) in rows and len(rows) == 3
        assert parse.holder_key("Bar", None) == "name:bar"
        assert parse.holder_key("Foo LLP", "/people/1/foo/") == "/people/1/foo/"


class Documents(unittest.TestCase):
    def test_documents(self):
        d = load("rel").documents
        types = {x.doc_type for x in d}
        assert {"annual_report", "concall_transcript", "concall_ppt", "credit_rating", "announcement"} <= types
        ar = [x for x in d if x.doc_type == "annual_report"]
        assert ar[0].title == "Annual Report 2026" and ar[0].source == "bse"
        assert any(x.source == "nse" for x in ar)           # old reports are on archives.nseindia.com
        ct = [x for x in d if x.doc_type == "concall_transcript"]
        assert ct[0].period_label == "Jul 2026"              # Oct 2026 transcript is not yet a link
        assert len({(x.doc_type, x.url) for x in d}) == len(d)
        assert any(x.doc_type == "credit_rating" and x.source == "other" for x in d)


class Schedules(unittest.TestCase):
    def test_schedule_json(self):
        js = json.dumps({"Material Cost %": {"Mar 2025": "65%", "Mar 2026": "65%", "TTM": "66%",
                                             "isExpandable": "Company.showSchedule(...)"},
                         "Raw Material": {"Mar 2026": "1,234"}})
        rows = parse.parse_schedule_json(js, "profit-loss", "Expenses", date(2026, 9, 30))
        m = rows[0]
        assert m.label == "Material Cost %" and m.unit == "pct" and m.schedule_parent == "Expenses"
        assert sorted(v[0] for v in m.values) == ["A", "A", "TTM"]
        assert (("TTM", date(2026, 9, 30), 66.0)) in m.values
        assert rows[1].unit == "cr" and rows[1].values == [("A", date(2026, 3, 31), 1234.0)]

    def test_quarter_and_bs_schedule(self):
        js = json.dumps({"X": {"Sep 2026": "5", "Jun 2026": "4"}})
        assert {v[0] for v in parse.parse_schedule_json(js, "quarters", "Sales", None)[0].values} == {"Q"}
        r = parse.parse_schedule_json(js, "balance-sheet", "Borrowings", None, 3)[0]
        assert {v[0] for v in r.values} == {"H"}


class Charts(unittest.TestCase):
    def test_valuation(self):
        js = json.dumps({"datasets": [
            {"metric": "Price to Earning", "label": "PE", "values": [["2026-10-30", 19.2], ["2026-10-29", None]]},
            {"metric": "Median PE", "label": "Median PE = 19.6", "values": [["2026-10-30", "19.6"]]},
            {"metric": "EV Multiple", "label": "x", "values": [["2026-10-30", None]]}]})
        out = parse.parse_chart_series(js)
        assert out == {"pe": [(date(2026, 10, 30), 19.2)], "median_pe": [(date(2026, 10, 30), 19.6)]}

    def test_price(self):
        js = json.dumps({"datasets": [
            {"metric": "Price", "values": [["2026-10-30", "1164.90"]]},
            {"metric": "DMA50", "values": [["2026-10-30", "1224.95"]]},
            {"metric": "DMA200", "values": [["2026-10-30", "1310.82"]]},
            {"metric": "Volume", "values": [["2026-10-30", 86481230, {"delivery": 48}]]}]})
        r = parse.parse_price_chart(js)
        assert r == [{"trade_date": date(2026, 10, 30), "close": 1164.9, "dma50": 1224.95, "dma200": 1310.82,
                      "volume": 86481230, "delivery_pct": 48.0}]


class Yf(unittest.TestCase):
    def setUp(self):
        import pandas as pd
        self.pd = pd
        from screener_scraper import yf
        self.yf = yf

    def test_statement_rows(self):
        pd, nan = self.pd, float("nan")
        df = pd.DataFrame({pd.Timestamp("2026-03-31"): [2.5e10, 61.0, 3.6e9, 0.25, nan],
                           pd.Timestamp("2025-03-31"): [2.0e10, 58.5, 3.6e9, 0.24, 5.0]},
                          index=["Total Revenue", "Diluted EPS", "Ordinary Shares Number", "Tax Rate For Calcs", "Foo"])
        rows = {(n, d): (u, v) for n, u, d, v in self.yf.statement_rows(df)}
        assert rows[("Total Revenue", date(2026, 3, 31))] == ("cr", 2500.0)       # 2.5e10 / 1e7
        assert rows[("Diluted EPS", date(2026, 3, 31))] == ("rs", 61.0)
        assert rows[("Ordinary Shares Number", date(2026, 3, 31))] == ("count", 3.6e9)
        assert rows[("Tax Rate For Calcs", date(2025, 3, 31))] == ("x", 0.24)
        assert ("Foo", date(2026, 3, 31)) not in rows and ("Foo", date(2025, 3, 31)) in rows   # NaN skipped
        assert self.yf.statement_rows(pd.DataFrame()) == []

    def test_price_rows_skip_zero_volume(self):
        pd = self.pd
        df = pd.DataFrame({"Open": [1, 2, 3], "High": [2, 3, 4], "Low": [1, 1, 1], "Close": [1.5, 2.5, 3.5],
                           "Adj Close": [1.4, 2.4, 3.4], "Volume": [100, 0, 300]},
                          index=pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"]).tz_localize("Asia/Kolkata"))
        rows = self.yf.price_rows(df, "ABC")
        assert [r[1] for r in rows] == [date(2020, 1, 1), date(2020, 1, 3)]
        assert rows[0] == ("ABC", date(2020, 1, 1), 1, 2, 1, 1.5, 1.4, 100)

    def test_actions_and_shares(self):
        pd = self.pd
        df = pd.DataFrame({"Dividends": [12.0, 0.0, 0.0], "Stock Splits": [0.0, 2.0, 0.0]},
                          index=pd.to_datetime(["2026-07-15", "2020-02-01", "2021-01-01"]).tz_localize("Asia/Kolkata"))
        rows = self.yf.action_rows(df, 7)
        assert (7, "dividend", date(2026, 7, 15), 12.0, None, "yfinance") in rows
        assert (7, "split", date(2020, 2, 1), 2.0, None, "yfinance") in rows and len(rows) == 2
        s = pd.Series([100, 110], index=pd.to_datetime(["2024-01-01", "2025-01-01"]))
        assert self.yf.shares_rows(s, 7) == [(7, date(2024, 1, 1), 100, "yfinance"), (7, date(2025, 1, 1), 110, "yfinance")]

    def test_snapshot(self):
        info = {"marketCap": 9.05e12, "beta": 0.18, "trailingPE": 17.5, "recommendationKey": "buy",
                "firstTradeDateMilliseconds": 1029123900000, "companyOfficers": [{"n": 1}], "executiveTeam": [],
                "fullTimeEmployees": 584519, "weird": float("nan"), "pegRatio": 3.4}
        v = self.yf.snapshot_values(info)
        assert v["market_cap"] == 9050000000000 and v["peg_ratio"] == 3.4 and v["recommendation_key"] == "buy"
        assert v["first_trade_date"] == date(2002, 8, 12) and v["full_time_employees"] == 584519
        assert "companyOfficers" not in v["info"] and "executiveTeam" not in v["info"] and v["info"]["weird"] is None
        assert self.yf.snapshot_values({}) is None

    def test_estimates_earnings_insiders(self):
        pd = self.pd
        eps = pd.DataFrame({"avg": [1.5, float("nan")], "numberOfAnalysts": [10, 9]}, index=["0q", "+1q"])
        rows = self.yf.estimate_rows(1, date(2026, 11, 1), {"eps": eps, "revenue": None}, {"mean": 2398.0, "low": None})
        assert (1, date(2026, 11, 1), "eps", "0q", "avg", 1.5) in rows
        assert (1, date(2026, 11, 1), "price_target", "current", "mean", 2398.0) in rows and len(rows) == 4
        e = pd.DataFrame({"EPS Estimate": [1.0, float("nan")], "Reported EPS": [1.1, float("nan")],
                          "Surprise(%)": [10.0, float("nan")]},
                         index=pd.to_datetime(["2026-10-08 06:00", "2027-01-11 05:00"]).tz_localize("America/New_York"))
        er = self.yf.earnings_rows(e, 1)
        assert er[0][1] == date(2026, 10, 8) and er[0][3] == 1.1 and er[1][3] is None
        ins = pd.DataFrame({"Shares": [10], "Insider": ["A B"], "Position": ["CEO"], "Transaction": ["Buy"],
                            "Text": ["x"], "Value": [100.0], "URL": [""], "Start Date": [pd.Timestamp("2026-01-01")]})
        r1, r2 = self.yf.insider_rows(ins, 1), self.yf.insider_rows(ins, 1)
        assert r1 == r2 and len(r1) == 1 and r1[0][2] == date(2026, 1, 1) and r1[0][7] == 10

    def test_nse_subjects(self):
        p = self.yf.parse_nse_subject
        assert p("Dividend - Rs 12 Per Share") == ("dividend", 12.0)
        assert p("Interim Dividend - Rs 5.50 Per Share") == ("dividend", 5.5)
        assert p("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share") == ("split", 5.0)
        assert p("Bonus 1:1") == ("bonus", 2.0)
        assert p("Bonus issue 3:2") == ("bonus", 2.5)
        assert p("Annual General Meeting") is None
        rows = self.yf.nse_action_rows([{"subject": "Dividend - Rs 12 Per Share", "exDate": "17-Oct-2025"},
                                        {"subject": "AGM", "exDate": "01-Jan-2025"}], 3)
        assert rows == [(3, "dividend", date(2025, 10, 17), 12.0, "Dividend - Rs 12 Per Share", "nse")]


class Throttle(unittest.TestCase):
    def _lim(self, **kw):
        from screener_scraper.http import RateLimiter
        self.now, self.slept = [0.0], []

        def sleep(s):
            self.slept.append(s)
            self.now[0] += s
        return RateLimiter(1.5, 0.0, 10.0, 20, clock=lambda: self.now[0], sleep=sleep, rand=lambda a, b: 0.0, **kw)

    def test_global_spacing(self):
        lim = self._lim()
        for _ in range(4):
            lim.acquire()
        assert abs(sum(self.slept) - 4.5) < 1e-9          # 4 requests -> 3 gaps of 1.5s after the first (+0)... slots at 0,1.5,3,4.5

    def test_429_doubles_caps_and_decays(self):
        lim = self._lim()
        lim.on_429(); assert lim.interval == 3.0
        lim.on_429(); lim.on_429(); lim.on_429(); assert lim.interval == 10.0
        for _ in range(19):
            lim.on_success()
        assert lim.interval == 10.0
        lim.on_success(); assert abs(lim.interval - 8.0) < 1e-9
        for _ in range(20 * 30):
            lim.on_success()
        assert lim.interval == 1.5

    def test_thread_safety_spacing(self):
        import threading, time
        from screener_scraper.http import RateLimiter
        lim = RateLimiter(0.05, 0.0)
        stamps = []
        def w():
            for _ in range(5):
                lim.acquire(); stamps.append(time.monotonic())
        ts = [threading.Thread(target=w) for _ in range(4)]
        [x.start() for x in ts]; [x.join() for x in ts]
        stamps.sort()
        assert min(b - a for a, b in zip(stamps, stamps[1:])) > 0.04


class Sharding(unittest.TestCase):
    def test_shards(self):
        from screener_scraper import config
        from screener_scraper.pipeline import shard_items, shard_ranked
        assert config.parse_shard("2/5") == (2, 5) and config.parse_shard(None) is None
        for bad in ("0/5", "6/5", "x"):
            try:
                config.parse_shard(bad); raise AssertionError(bad)
            except ValueError:
                pass
        rows = [(r, f"S{r}") for r in range(1, 101)]
        parts = [shard_ranked(rows, (k, 5)) for k in range(1, 6)]
        assert all(len(p) == 20 for p in parts) and parts[0][0][0] == 1 and parts[1][0][0] == 2
        assert sorted(r for p in parts for r, _ in p) == list(range(1, 101))
        syms = ["TCS", "ABB", "INFY", "WIPRO", "SBIN"]
        got = [shard_items(syms, (k, 2)) for k in (1, 2)]
        assert got == [["ABB", "SBIN", "WIPRO"], ["INFY", "TCS"]]

    def test_coverage_flags(self):
        from screener_scraper.__main__ import coverage_flags
        assert coverage_flags({"daily_from": None, "screener_facts": 5}) [0] == "NO_DAILY_PRICES"


class EntryPoints(unittest.TestCase):
    def test_wide_valuation(self):
        from screener_scraper.pipeline import pivot_valuation, VAL_COLUMNS
        rows = pivot_valuation({"pe": [(date(2026, 1, 1), 10.0), (date(2026, 1, 2), 11.0)],
                                "median_pe": [(date(2026, 1, 2), 9.5)], "new_thing": [(date(2026, 1, 1), 3.0)]})
        assert [r[0] for r in rows] == [date(2026, 1, 1), date(2026, 1, 2)]
        assert len(rows[0]) == 1 + len(VAL_COLUMNS) + 1
        assert rows[0][1] == 10.0 and rows[0][2] is None and rows[1][2] == 9.5
        assert rows[0][-1] is not None and rows[1][-1] is None          # unknown series -> extra jsonb

    def test_exceptions(self):
        import requests
        from screener_scraper.http import FetchError, HTTPFailure
        from screener_scraper.pipeline import TaskResult, _raise_for_tasks
        e = FetchError("HTTP 429 for x", 429, 5)
        assert isinstance(e, requests.HTTPError) and e.status_code == 429
        for exc, want in ((FetchError("x", 403, 1), FetchError), (FetchError("x", None, 5), requests.ConnectionError),
                          (parse.ParseError("bad"), parse.ParseError), (KeyError("k"), parse.ParseError)):
            tk = TaskResult("schedules:C", "failed", exc=exc, error="x")
            try:
                _raise_for_tasks([TaskResult("page:C"), tk])
                raise AssertionError("should raise")
            except want:
                pass
        try:
            parse.parse_page("<html><body>Access denied</body></html>")
            raise AssertionError
        except parse.ParseError:
            pass
        try:
            parse.parse_schedule_json("<html>", "profit-loss", "Sales", None)
            raise AssertionError
        except parse.ParseError:
            pass


if __name__ == "__main__":
    unittest.main()
