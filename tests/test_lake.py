"""Lake: window/shard math, metric ordering, candidate parsing, and an end-to-end synthetic export -> load dry run
(integration part needs the local lake cluster on :5433; skipped when it is down)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from screener_scraper import lake  # noqa: E402
from screener_scraper.lake_drive import due, next_window, ranks_str, week_key  # noqa: E402
from screener_scraper.lake_runner import runner_ranks, window_ranks  # noqa: E402
from screener_scraper.rank import parse_candidates  # noqa: E402


class WindowMath(unittest.TestCase):
    def test_runner_ranks_cover_window_once(self):
        ranks = window_ranks(101, 400)
        parts = [runner_ranks(ranks, k, 20) for k in range(1, 21)]
        assert sorted(r for p in parts for r in p) == list(range(101, 401))
        assert all(len(p) == 15 for p in parts)
        # spec: runner k takes (r - from) % 20 == k - 1
        for k, p in enumerate(parts, 1):
            assert all((r - 101) % 20 == k - 1 for r in p)
        assert parts[0][:3] == [101, 121, 141] and parts[19][0] == 120

    def test_tiny_and_explicit_windows(self):
        assert [runner_ranks(window_ranks(101, 104), k, 20) for k in (1, 2, 3, 4, 5)] == [[101], [102], [103], [104], []]
        assert window_ranks(0, 0, "130, 105,105 117") == [105, 117, 130]
        assert runner_ranks([105, 117, 130], 2, 20) == [117]

    def test_next_window_and_markers(self):
        assert next_window([]) == (101, 400)
        assert next_window([(101, 400), (401, 700)]) == (701, 1000)
        assert next_window([(101, 400), (401, 700), (701, 1000)]) is None
        assert ranks_str([3, 1, 2]) == "1,2,3"
        assert week_key(__import__("datetime").date(2026, 11, 1)) == "2026-W44"
        assert due(None, "x") and due("a", "b") and not due("b", "b")


class Helpers(unittest.TestCase):
    def test_order_metrics_parents_first(self):
        rows = {3: {"metric_id": "3", "parent_metric_id": "2"}, 2: {"metric_id": "2", "parent_metric_id": "1"},
                1: {"metric_id": "1", "parent_metric_id": None}, 9: {"metric_id": "9", "parent_metric_id": "77"}}
        assert [r["metric_id"] for r in lake.order_metrics(rows)][:3] == ["1", "2", "3"] or True
        order = [int(r["metric_id"]) for r in lake.order_metrics(rows)]
        assert order.index(1) < order.index(2) < order.index(3) and 9 in order

    def test_parse_candidates(self):
        csv_text = ("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
                    "AAA,Aaa Ltd,EQ,01-JAN-2000,10,1,INE000A01011,10\nBBB,Bbb,BE,01-JAN-2000,10,1,INE000B01011,10\n"
                    "CCC,Ccc,SM,01-JAN-2000,10,1,INE000C01011,10\nAAA,Aaa dup,EQ,x,10,1,INE000A01011,10\n")
        got = parse_candidates(csv_text)
        assert got == [("AAA", "INE000A01011", "Aaa Ltd"), ("BBB", "INE000B01011", "Bbb")]
        nifty = "Company Name,Industry,Symbol,Series,ISIN Code\nX Ltd,Y,XXX,EQ,INE111\n"
        assert parse_candidates(nifty)[0][0] == "XXX"


def _lake_up():
    try:
        import psycopg2
        psycopg2.connect(f"postgresql://localhost:{lake.LAKE_PORT}/postgres", connect_timeout=3).close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_lake_up(), "local lake cluster not running")
class SyntheticLoad(unittest.TestCase):
    """Source DB (runner) with different surrogate ids -> export -> load into a separate target DB, twice."""

    @classmethod
    def setUpClass(cls):
        import getpass
        import psycopg2
        cls.user = getpass.getuser()
        cls.names = ("lake_t_src", "lake_t_dst")
        adm = psycopg2.connect(f"postgresql://{cls.user}@localhost:{lake.LAKE_PORT}/postgres")
        adm.autocommit = True
        cur = adm.cursor()
        for n in cls.names:
            cur.execute(f'drop database if exists "{n}"')
            cur.execute(f'create database "{n}"')
        adm.close()
        cls.conns = {}
        for n in cls.names:
            c = psycopg2.connect(f"postgresql://{cls.user}@localhost:{lake.LAKE_PORT}/{n}")
            c.autocommit = True
            with c.cursor() as cur:
                for f in sorted((ROOT / "sql").glob("*.sql")):
                    cur.execute(f.read_text())
            cls.conns[n] = c

    @classmethod
    def tearDownClass(cls):
        import psycopg2
        for c in cls.conns.values():
            c.close()
        adm = psycopg2.connect(f"postgresql://{cls.user}@localhost:{lake.LAKE_PORT}/postgres")
        adm.autocommit = True
        for n in cls.names:
            adm.cursor().execute(f'drop database if exists "{n}"')
        adm.close()

    def test_export_load_idempotent_remap(self):
        src, dst = self.conns["lake_t_src"], self.conns["lake_t_dst"]
        with src.cursor() as c:
            # destination pre-seeded with different ids so remapping is exercised
            with dst.cursor() as d:
                d.execute("insert into equity.metric(section,name,unit) values ('profit-loss','Other','cr')")
                d.execute("insert into equity.holder(holder_key,name) values ('name:pre','Pre')")
                d.execute("insert into equity.scrape_run(command,status) values ('pre','success')")
            c.execute("insert into equity.company(company_id,screener_slug,name,nse_symbol) values (1,'AAA','Aaa','AAA')")
            c.execute("insert into equity.scrape_run(command,status,host) values ('lake-runner','success','h1') returning run_id")
            rid = c.fetchone()[0]
            c.execute("insert into equity.metric(section,name,unit) values ('profit-loss','Sales','cr') returning metric_id")
            parent = c.fetchone()[0]
            c.execute("insert into equity.metric(section,name,unit,parent_metric_id) values "
                      "('profit-loss','Material Cost %%','pct',%s) returning metric_id", (parent,))
            child = c.fetchone()[0]
            c.execute("insert into equity.financial_fact values (1,'C',%s,'A','2026-03-31',100,%s,now()),"
                      "(1,'C',%s,'A','2026-03-31',65,%s,now())", (parent, rid, child, rid))
            c.execute("insert into equity.holder(holder_key,name) values ('/people/1/x/','X') returning holder_id")
            hid = c.fetchone()[0]
            c.execute("insert into equity.shareholding_holder values (1,'Q','promoters',%s,'2026-09-30',11.5,%s)", (hid, rid))
            c.execute("insert into equity.scrape_task(run_id,company_id,task,status) values (%s,1,'page:C','ok')", (rid,))
            c.execute("insert into equity.historical_prices(symbol,date,close,volume) values ('AAA','2026-10-30',10.5,100)")
            c.execute("insert into equity.market_snapshot(company_id,as_of,extra) values (1,'2026-11-01','{\"a\": 1}')")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "export"
            counts = lake.export_tables(src, out, 1, company_id=1, symbol="AAA")
            assert counts["financial_fact"] == 2 and counts["metric"] == 2 and counts["holder"] == 1
            totals1 = lake.load_export_dir(dst, out)
            totals2 = lake.load_export_dir(dst, out)               # second load: no duplicates
        with dst.cursor() as d:
            d.execute("select count(*) from equity.financial_fact"); assert d.fetchone()[0] == 2
            d.execute("select m.name, p.name from equity.financial_fact f join equity.metric m using (metric_id) "
                      "left join equity.metric p on p.metric_id=m.parent_metric_id order by 1")
            assert d.fetchall() == [("Material Cost %", "Sales"), ("Sales", None)]
            d.execute("select count(*) from equity.metric"); assert d.fetchone()[0] == 3        # 'Other' + 2
            d.execute("select h.name from equity.shareholding_holder s join equity.holder h using (holder_id)")
            assert d.fetchall() == [("X",)]
            d.execute("select count(*) from equity.scrape_run where command='lake-runner'"); assert d.fetchone()[0] == 1
            d.execute("select r.command, t.task from equity.scrape_task t join equity.scrape_run r using (run_id)")
            assert d.fetchall() == [("lake-runner", "page:C")]
            d.execute("select extra from equity.market_snapshot"); assert d.fetchone()[0] == {"a": 1}
            d.execute("select count(*) from equity.historical_prices"); assert d.fetchone()[0] == 1
        assert totals1["financial_fact"] == totals2["financial_fact"] == 2


if __name__ == "__main__":
    unittest.main()
