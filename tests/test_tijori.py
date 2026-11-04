import unittest
from datetime import date

from screener_scraper import tijori as T


class Tijori(unittest.TestCase):
    def test_slugs(self):
        self.assertEqual(T.name_slugs("Maruti Suzuki India Ltd")[0], "maruti-suzuki-india-limited")
        self.assertIn("zomato-ltd", T.name_slugs("Zomato Ltd"))
        self.assertEqual(T.name_slugs("Mahindra & Mahindra Ltd")[0], "mahindra-and-mahindra-limited")
        xml = ("<url><loc>https://www.tijorifinance.com/company/icici-bank-limited/</loc></url>"
               "<url><loc>https://www.tijorifinance.com/company/icici-bank-limited/financials/</loc></url>")
        self.assertEqual(T.parse_sitemap(xml), {"icici-bank-limited": "https://www.tijorifinance.com/company/icici-bank-limited/"})

    def test_files_and_quarters(self):
        html = ('<a href="https://files.tijorifinance.com/insight/india/4470/Conference%20Call/CC-Mar16.pdf">'
                '<a href="https://files.tijorifinance.com/insight/india/4470/Annual%20Report/AR-21.pdf">'
                '<a href="https://files.tijorifinance.com/insight/india/4470/Conference%20Call/CC-Mar16.pdf">')
        self.assertEqual(T.parse_files(html), [
            ("CC", "https://files.tijorifinance.com/insight/india/4470/Conference%20Call/CC-Mar16.pdf", 3, 2016),
            ("AR", "https://files.tijorifinance.com/insight/india/4470/Annual%20Report/AR-21.pdf", None, 2021)])
        self.assertEqual(T.cc_quarters(3, 2016), [date(2016, 3, 31), date(2015, 12, 31)])   # quarter-end naming first
        self.assertEqual(T.cc_quarters(1, 2026), [date(2025, 12, 31), date(2026, 3, 31)])   # a January call: Q3

    def test_verification(self):
        tr = ["Maruti Suzuki India Limited Q4 FY16 Earnings Conference Call April 26, 2016 Moderator: Ladies and "
              "gentlemen, good day and welcome " + "x " * 3000]
        self.assertIsNone(T.check_transcript(tr, "Maruti Suzuki India Ltd", date(2016, 3, 31)))
        self.assertIn("quarter", T.check_transcript(tr, "Maruti Suzuki India Ltd", date(2016, 9, 30)))
        self.assertIn("title does not name", T.check_transcript(tr, "Tata Motors Ltd", date(2016, 3, 31)))
        pru = ["ICICI Prudential Life Insurance Company Earnings Conference call - Quarter ended September 30, 2016 "
               "(Q2-2017) October 25, 2016 " + "y " * 400 + "offer for sale by ICICI Bank " + "x " * 3000]
        self.assertIsNotNone(T.check_transcript(pru, "ICICI Bank Ltd", date(2016, 9, 30), ["icici prudential"]))
        early = ["ICICI Prudential Life (an ICICI Bank company) Q2-2017 Earnings Conference Call " + "x " * 3000]
        self.assertIn("group company", T.check_transcript(early, "ICICI Bank Ltd", date(2016, 9, 30),
                                                          ["icici prudential", "icici lombard"]))
        self.assertIn("title does not name", T.check_transcript(pru, "ICICI Bank Ltd", date(2016, 9, 30)))
        bank = ["ICICI Bank Limited Q2-2017 Earnings Conference Call October 24, 2016 Moderator: Ladies and gentlemen "
                + "x " * 3000]
        self.assertIsNone(T.check_transcript(bank, "ICICI Bank Ltd", date(2016, 9, 30), ["icici prudential"]))
        self.assertIsNone(T.title_names("Mahindra & Mahindra Ltd. Q2 FY25 earnings call", "Mahindra & Mahindra Ltd"))
        zom = ["Zomato Limited Q3 FY24 Earnings Conference Call February 8, 2024 Moderator " + "x " * 3000]
        self.assertIsNone(T.check_transcript(zom, ["Eternal Ltd", "Zomato Limited"], date(2023, 12, 31)))
        self.assertIsNotNone(T.check_transcript(zom, "Eternal Ltd", date(2023, 12, 31)))
        ar = ["Eternal Limited (formerly Zomato) Annual Report 2020-21"]
        self.assertIsNone(T.check_annual_report(ar, "Eternal Ltd", 2021))
        self.assertIn("fiscal year", T.check_annual_report(ar, "Eternal Ltd", 2019))
        self.assertEqual(T.name_tokens("JSW Steel Ltd"), ["jsw", "steel"])


if __name__ == "__main__":
    unittest.main()
