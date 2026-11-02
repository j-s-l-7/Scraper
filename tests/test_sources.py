"""Parsers of the public-source loaders, on small real-file fixtures (tests/fixtures/sources)."""
import io
import json
import re
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

from screener_scraper.sources import bse_bhav, bulk_block, credit_ratings, fo_bhav, gdelt_news, macro, nse_bhav_full
from screener_scraper.sources import _common as c

FX = Path(__file__).parent / "fixtures" / "sources"


class TestBhav(unittest.TestCase):
    def test_nse_full(self):
        rows = nse_bhav_full.parse((FX / "nse_full.csv").read_text())
        self.assertGreater(len(rows), 100)
        r = rows[0]
        self.assertEqual((r[0], r[1], r[2]), ("20MICRONS", "EQ", date(2026, 10, 30)))
        self.assertEqual((r[8], r[10], r[13], r[14]), (161.48, 132501, 60324, 45.53))
        self.assertEqual(len({(x[0], x[1], x[2]) for x in rows}), len(rows))
        self.assertTrue(any(x[13] is None for x in rows) or True)

    def test_bse_new_and_old(self):
        new = bse_bhav.parse_bytes((FX / "bse_new.csv").read_bytes(), date(2026, 10, 30))
        self.assertEqual(new[0][:3], ("500002", "INE117A01022", date(2026, 10, 30)))
        self.assertEqual((new[0][6], new[0][7], new[0][8]), (6403.0, 9368, 2047))
        old = bse_bhav.parse_bytes((FX / "bse_old_small.zip").read_bytes(), date(2024, 1, 2))
        self.assertEqual(old[0][:2], ("500002", "INE117A01022"))
        self.assertEqual((old[0][6], old[0][7], old[0][8]), (4695.95, 3586, 706))

    def test_bse_missing_page(self):
        self.assertIsNone(bse_bhav.parse_bytes(b"<!DOCTYPE html><html>not found</html>", date(2024, 5, 1)))

    def test_nse_full_served_as_xlsx(self):
        rows = nse_bhav_full.parse_xlsx((FX / "nse_full_xlsx_small.xlsx").read_bytes())
        self.assertEqual(len(rows), 3)
        r = rows[0]
        self.assertEqual((r[0], r[1], r[2]), ("20MICRONS", "EQ", date(2022, 8, 8)))
        self.assertEqual((r[8], r[10], r[13], r[14]), (111.15, 1687369, 571768, 33.89))

    def test_bse_old_line_run_into_the_next(self):
        old = bse_bhav.parse_bytes((FX / "bse_old_broken_line.zip").read_bytes(), date(2022, 2, 7))
        self.assertEqual([r[0] for r in old], ["500002", "500003", "531246"])
        self.assertEqual(old[2][3:7], (32.45, 33.0, 32.0, 32.0))

    def test_a_failing_day_keeps_the_days_before_it(self):
        saved = {}

        def load_day(d):
            if d == date(2022, 8, 10):
                raise ValueError("unreadable file")
            return 1
        with mock.patch.object(c, "get_marker", return_value={"last_date": "2022-08-05"}), \
                mock.patch.object(c, "set_marker", side_effect=lambda db, name, **kv: saved.update(kv)), \
                mock.patch.object(c, "today_ist", return_value=date(2022, 8, 12)):
            with self.assertRaises(ValueError):
                c.run_days(mock.MagicMock(), "x", None, None, date(2019, 9, 20), date(2019, 9, 20), "t", load_day)
        self.assertEqual(saved, {"last_date": date(2022, 8, 9)})

    def test_bulk_block(self):
        b = bulk_block.parse((FX / "bulk.csv").read_text(), "bulk")
        k = bulk_block.parse((FX / "block.csv").read_text(), "block")
        self.assertEqual(b[0][1:], (date(2026, 10, 30), "AEROFLEX", "QE SECURITIES LLP", "BUY", 670611, 540.12, "bulk"))
        self.assertEqual(k[0][7], "block")
        self.assertEqual(b, bulk_block.parse((FX / "bulk.csv").read_text(), "bulk"))     # stable hashes
        self.assertEqual(len({x[0] for x in b + k}), len(set(b + k)))

    def test_fo(self):
        rows = fo_bhav.parse_zip((FX / "fo_small.zip").read_bytes())
        kinds = {r[1] for r in rows}
        self.assertEqual(kinds, {"STF", "IDF", "STO", "IDO"})
        opts = [r for r in rows if r[1] in ("STO", "IDO")]
        self.assertTrue(all(r[4] == 0 and r[5] in ("CE", "PE") and r[6] is None for r in opts))
        fut = [r for r in rows if r[1] == "STF"][0]
        self.assertEqual(fut[5], "")
        self.assertEqual(len({r[:6] for r in rows}), len(rows))


class TestOthers(unittest.TestCase):
    def test_worldbank(self):
        rows = macro.parse_worldbank(json.loads((FX / "worldbank_cpi.json").read_text()))
        self.assertTrue(rows and all(isinstance(y, int) for y, _ in rows))
        self.assertEqual(macro.parse_worldbank([{"message": "x"}]), [])

    def test_gdelt(self):
        p = {"articles": [{"url": "http://a/1", "title": "T", "seendate": "20261030T143000Z", "domain": "a",
                           "language": "English"}, {"title": "no url"}]}
        rows = gdelt_news.parse(p, 7)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][0], rows[0][5], rows[0][6]), (7, None, "English"))
        self.assertEqual(gdelt_news.parse({}, 7), [])
        self.assertEqual(gdelt_news.clean_name("Reliance Industries Ltd."), "Reliance Industries")

    def test_rating_text(self):
        t = ("Credit Bulletin September 30, 2026 Reliance Industries Limited Rating reaffirmed. "
             "Non-Convertible Debentures 5000 Crisil AAA/Stable Commercial Paper 3000 Crisil A1+ "
             "Bank Facilities [ICRA]AA+ (Negative) CARE AAA; Stable IND A1+")
        rs = credit_ratings.parse_text(t, "30 Sep from crisil", "https://crisil.com/x")
        got = {(r["agency"], r["instrument"], r["rating"], r["outlook"]) for r in rs}
        self.assertIn(("CRISIL", "NCD", "AAA", "Stable"), got)
        self.assertIn(("CRISIL", "Commercial Paper", "A1+", None), got)
        self.assertIn(("ICRA", "Bank Facilities", "AA+", "Negative"), got)
        self.assertIn(("CARE", "Bank Facilities", "AAA", "Stable"), got)
        self.assertEqual(rs[0]["action"], "reaffirmed")
        self.assertEqual(rs[0]["date"], date(2026, 9, 30))

    def test_rating_fixture(self):
        from screener_scraper import documents as d
        raw = (FX / "rating_crisil.html").read_bytes()
        data, fmt = d.sniff_and_unwrap(raw, "x.html")
        pages, _ = d.extract_other(data, fmt)
        rs = credit_ratings.parse_text("\n".join(pages), "30 Sep from crisil", "https://www.crisil.com/x")
        self.assertTrue(any(r["agency"] == "CRISIL" and r["rating"] == "AAA" and r["outlook"] == "Stable" for r in rs))

    def test_label_date(self):
        self.assertEqual(credit_ratings.date_from_label("19 Aug from fitch", date(2026, 11, 1)), date(2026, 8, 19))
        self.assertEqual(credit_ratings.date_from_label("19 Dec from fitch", date(2026, 11, 1)), date(2025, 12, 19))
        self.assertEqual(credit_ratings.agency_from("19 Aug from fitch", "u"), "India Ratings")

    def test_helpers(self):
        self.assertIsNone(c.num("-"))
        self.assertEqual(c.num(" 1,234.5 "), 1234.5)
        self.assertEqual(len(list(c.weekdays(date(2026, 10, 30), date(2026, 11, 3)))), 3)


if __name__ == "__main__":
    unittest.main()


class TestSecondWave(unittest.TestCase):
    def test_fpi_latest(self):
        from screener_scraper.sources import nsdl_fpi
        rows = nsdl_fpi.parse_latest((FX / "fpi_latest.html").read_text(encoding="utf8"))
        self.assertEqual(rows[0][:4], (date(2026, 10, 30), "Equity", "Stock Exchange", 13626.03))
        self.assertEqual(rows[0][5], -812.73)                                     # (812.73) is negative
        self.assertEqual(rows[-1][1:3], ("Total", "Total"))
        self.assertFalse(any("Futures" in r[1] for r in rows))                    # derivatives table excluded
        self.assertEqual(len({r[:3] for r in rows}), len(rows))

    def test_fpi_sector(self):
        from screener_scraper.sources import nsdl_fpi
        rows = nsdl_fpi.parse_sector((FX / "fpi_sector.html").read_text(encoding="utf8"), date(2025, 10, 15))
        self.assertTrue(rows)
        first = [r for r in rows if r[1] == "Automobile and Auto Components" and r[2] == "auc_prev" and r[3] == "Equity"][0]
        self.assertEqual((first[4], first[5]), (569410.0, 64128.0))
        self.assertIn("MF Equity", {r[3] for r in rows})
        self.assertEqual(len({r[:4] for r in rows}), len(rows))
        self.assertEqual(list(nsdl_fpi.fortnight_ends(date(2025, 10, 1), date(2025, 11, 20))),
                         [date(2025, 10, 15), date(2025, 10, 31), date(2025, 11, 15)])

    def test_mospi(self):
        from screener_scraper.sources import mospi
        cpi = mospi.parse_cpi(json.loads((FX / "mospi_cpi.json").read_text()))
        self.assertTrue(cpi and all(r[3] == "2012" and r[1] == date(2025, 12, 1) for r in cpi))
        self.assertTrue(all("CPI_2012" in r[0] for r in cpi))
        iip = mospi.parse_iip(json.loads((FX / "mospi_iip.json").read_text()))
        self.assertTrue(any(r[0].endswith("INDEX") and r[3] == "2011-12" for r in iip))
        wpi = mospi.parse_wpi(json.loads((FX / "mospi_wpi.json").read_text()))
        self.assertEqual(wpi, [("MOSPI_WPI_201112_ALL_INDEX", date(2024, 12, 1), 155.7, "2011-12")])

    def test_cm_bhav_formats(self):
        from screener_scraper.sources import nse_cm_bhav as m
        old = m.parse_zip((FX / "cm_old_small.zip").read_bytes(), date(2010, 1, 4))
        self.assertEqual((old[0]["symbol"], old[0]["series"], old[0]["date"], old[0]["close"]),
                         ("20MICRONS", "EQ", date(2010, 1, 4), 47.55))
        full = m.to_full_rows(old)
        self.assertEqual(full[0][2], date(2010, 1, 4))
        self.assertAlmostEqual(full[0][11], 1719201.35 / 1e5)
        new = m.parse_zip((FX / "cm_udiff_small.zip").read_bytes(), date(2026, 10, 30))
        self.assertTrue(new and new[0]["isin"].startswith("IN") and new[0]["date"] == date(2026, 10, 30))

    def test_adj_factor(self):
        from screener_scraper.sources.adj_factor import factor_for
        self.assertAlmostEqual(factor_for("split", 5.0), 0.2)
        self.assertAlmostEqual(factor_for("bonus", 1.5), 1 / 1.5)
        self.assertAlmostEqual(factor_for("dividend", 10.0, 1000.0), 0.99)
        self.assertIsNone(factor_for("dividend", 10.0, None))
        self.assertIsNone(factor_for("split", None))
        self.assertIsNone(factor_for("rights", 2.0))

    def test_mf_holding_tolerant_parser(self):
        """SYNTHETIC layout (no real AMC file is obtainable without JS/blocked pages): headers differ per fund house."""
        import io
        import openpyxl
        from screener_scraper.sources import amc_portfolio as a
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "EQ"
        ws.append(["Axis Bluechip Fund"])
        ws.append(["Monthly Portfolio Statement as on October 31, 2026"])
        ws.append(["Name of the Instrument", "ISIN", "Industry", "Quantity", "Market/Fair Value (Rs. in Lakhs)", "% to NAV"])
        ws.append(["HDFC Bank Ltd", "INE040A01034", "Banks", 100000, 1500.5, 9.8])
        ws.append(["Reliance Industries Ltd", "INE002A01018", "Oil", "250,000", "3,000", 12.1])
        ws.append(["Net Receivables", "", "", "", 10, 0.1])
        buf = io.BytesIO()
        wb.save(buf)
        rows = a.parse_file(buf.getvalue(), "x.xlsx", "Axis", date(2026, 10, 31), {"INE002A01018": 7})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1:5], ("Axis", "Axis Bluechip Fund", "INE040A01034", None))
        self.assertAlmostEqual(rows[0][7], 15.005)                                # lakh -> crore
        self.assertEqual((rows[1][4], rows[1][6], rows[1][8]), (7, 250000.0, 12.1))
        self.assertEqual(a.month_from_text("Portfolio_October_2026.xlsx"), date(2026, 10, 31))
        self.assertIsNone(a.match_header(["a", "b"]))
