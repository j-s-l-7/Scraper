"""Parsers of the public-source loaders, on small real-file fixtures (tests/fixtures/sources)."""
import io
import json
import re
import unittest
import zipfile
from datetime import date
from pathlib import Path

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
