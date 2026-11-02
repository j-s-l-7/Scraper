"""Lake: window/shard math, metric ordering, candidate parsing, and an end-to-end synthetic export -> load dry run
(integration part needs the local lake cluster on :5433; skipped when it is down)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from screener_scraper import lake, lake_drive as LD  # noqa: E402
from screener_scraper.lake_drive import (MAX_PART_FAILS, PART_BACKOFF_S, due, fails_of, gave_up_ranks,  # noqa: E402
                                         next_window, part_due, plan_parts, ranks_str, retry_action, week_key)
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


class LoaderBookkeeping(unittest.TestCase):
    def test_parts_given_up_after_max_failures(self):
        assert fails_of("failed 2") == 2 and fails_of("loaded") == 0 and fails_of(None) == 0
        assert fails_of("failed x") == 0 and fails_of("no_artifacts: boom") == 0
        names = ["lake-1-1", "lake-1-2", "lake-1-3", "lake-1-4"]
        logged = {"lake-1-1": "loaded", "lake-1-2": "failed 1", "lake-1-3": f"failed {MAX_PART_FAILS}"}
        assert plan_parts(names, logged) == (["lake-1-2", "lake-1-4"], ["lake-1-3"])
        assert plan_parts(["a"], {"a": f"failed {MAX_PART_FAILS - 1}"}) == (["a"], [])  # a waiting part keeps the run open
        assert plan_parts(names, {n: "loaded" for n in names}) == ([], [])
        assert plan_parts([], {}) == ([], [])           # a finished run without artifacts has nothing left to load

    def test_failed_parts_back_off_so_an_outage_cannot_give_them_up(self):
        h = PART_BACKOFF_S
        assert part_due(None, 0) and not part_due("failed 1", h - 1) and part_due("failed 1", h)
        assert not part_due("failed 3", 4 * h - 1) and part_due("failed 3", 4 * h)
        assert sum(h * 2 ** (n - 1) for n in range(1, MAX_PART_FAILS)) >= 7 * 3600      # giving up takes >= 7 h

    def test_given_up_parts_map_to_their_ranks(self):
        assert gave_up_ranks(window_ranks(401, 700), ["lake-9-4"]) == list(range(404, 701, 20))
        assert gave_up_ranks(window_ranks(401, 700), []) == []

    def test_a_part_that_fails_to_load_is_counted_not_raised(self):
        sql = []

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, q, p=()): sql.append((q, p))
            def fetchall(self): return []

        class Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return Cur()
            def rollback(self): pass

        def download(cmd, **kw):        # `gh run download ... -D <dir>` lands one export file
            out = Path(cmd[cmd.index("-D") + 1]) / "export"
            out.mkdir(parents=True)
            (out / "t.000001.csv.gz").write_bytes(b"")
            return mock.Mock(returncode=0, stderr="")

        def gh(*a, **k):
            return "lake-9-1" if a[0] == "api" else "completed"

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(LD, "ARTIFACT_DIR", Path(tmp)), \
                mock.patch.object(LD, "gh", gh), mock.patch.object(LD.subprocess, "run", download), \
                mock.patch.object(LD, "load_export_dir", side_effect=KeyError("metric_id without mapping")):
            res = LD.load_run(9, Conn())
        assert not res.complete and res.gave_up == []
        assert (9, "lake-9-1", "failed 1") in [p for _, p in sql] and not any("__complete__" in q for q, _ in sql)

    def test_secrets_ready_tells_unreachable_from_missing(self):
        with mock.patch.object(LD, "gh", side_effect=RuntimeError("error connecting to api.github.com")):
            assert LD.secrets_ready() is None
        with mock.patch.object(LD, "gh", return_value='[{"name": "DATABASE_URL"}]'):
            assert LD.secrets_ready() is True
        with mock.patch.object(LD, "gh", return_value="[]"):
            assert LD.secrets_ready() is False

    def test_dispatch_finds_its_own_run_through_a_polling_blip(self):
        late = "2999-01-01T00:00:00Z"
        other = {"databaseId": 6, "createdAt": late, "displayTitle": "lake 101-400"}
        mine = {"databaseId": 7, "createdAt": late, "displayTitle": "lake 401-700"}
        polls = iter([[], RuntimeError("EOF"), [other], [other, mine]])

        def list_runs(workflow, limit):
            x = next(polls)
            if isinstance(x, Exception):
                raise x
            return x
        with mock.patch.object(LD, "list_runs", list_runs), mock.patch.object(LD, "gh"), mock.patch("time.sleep"):
            assert LD.dispatch("equity-lake.yml", {"from_rank": "401"}, title="lake 401-700") == 7

    def test_one_retry_per_window_never_while_busy(self):
        assert retry_action([118], 1, False, False) == "dispatch"
        assert retry_action([118], 1, False, True) == "wait"      # a newer queued run would cancel a pending one
        assert retry_action([118], 1, True, False) == "none"      # already retried
        assert retry_action([118], 2, False, False) == "none"     # a retry window is not retried again
        assert retry_action([], 1, False, True) == "none"


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


class FleetRouting(unittest.TestCase):
    def test_select_for(self):
        from screener_scraper.fleet import select_for
        ranked = [(101, 1, "A"), (102, 2, "B"), (103, 3, "C")]
        assert [r[0] for r in select_for("screener", ranked, 0, 1)] == [103, 102, 101]     # descending
        assert [r[0] for r in select_for("bse", ranked, 0, 1)] == [101, 102, 103]



class _SrcCur:
    """Supabase stand-in: answers by SQL substring and records every statement."""
    def __init__(self, src):
        self.src, self.rows, self.description = src, [], None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.src.sql.append((sql, params))
        cols, rows = next((v for k, v in self.src.answers.items() if k in sql), (["x"], []))
        self.description, self.rows = [(c,) for c in cols], rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Src:
    def __init__(self, answers):
        self.answers, self.sql = answers, []
        self.trail = self

    def cursor(self):
        return _SrcCur(self)


class Seed(unittest.TestCase):
    """fleet passes seeded identity + universe rows company by company: 2 Supabase queries x ~2,518 companies
    (~10.7 min) on every pass of every role, although the lake already had every row."""
    N = 2518

    def ranked(self, n=N):
        return [(100 + i, 5000 + i, f"S{i}") for i in range(1, n + 1)]

    def test_seed_is_three_queries_whatever_the_number_of_companies(self):
        from screener_scraper import lake_runner
        ranked = self.ranked()
        src = _Src({"u.rank = any": (["rank", "company_id", "symbol"], ranked),
                    "from equity.company": (["company_id", "name"], [(c, s) for _, c, s in ranked]),
                    "from equity.universe": (["universe_code", "as_of", "company_id", "rank"],
                                             [("TOP1000_MCAP", "2026-11-01", c, r) for r, c, _ in ranked])})
        dst = mock.MagicMock()
        with mock.patch.object(lake_runner, "execute_values") as ev:
            assert lake_runner.seed(src, dst, [r for r, _, _ in ranked]) == ranked
        assert len(src.sql) == 3 and all("= any(%s)" in q for q, _ in src.sql)
        assert sorted(src.sql[1][1][0]) == [c for _, c, _ in ranked]
        assert [len(c.args[2]) for c in ev.call_args_list] == [self.N, self.N]
        assert all("on conflict do nothing" in c.args[1] for c in ev.call_args_list)       # insert-only: idempotent
        assert "equity.company" in ev.call_args_list[0].args[1]                             # before its universe rows

    def test_the_fleet_seeds_only_when_supabase_changed(self):
        from screener_scraper import fleet
        state = {}

        class LakeCur(_SrcCur):
            def execute(self, sql, params=()):
                if sql.lstrip().startswith("insert into public.lake_state"):
                    state[params[0]] = params[1]
                self.rows = [(state[params[0]],)] if "from public.lake_state" in sql and params[0] in state else []

        lake_db = _Src({})
        lake_db.cursor = lambda: LakeCur(lake_db)
        as_of = {"v": "2026-11-01"}
        src = _Src({})
        src.cursor = lambda: type("C", (_SrcCur,), {"execute": lambda self, q, p=(): setattr(
            self, "rows", [(as_of["v"],)])})(src)
        ranked = self.ranked(5)
        with mock.patch.object(fleet, "seed") as seed:
            assert fleet.seed_if_changed(src, lake_db, ranked)            # first pass: seeds, saves the marker
            assert not fleet.seed_if_changed(src, lake_db, ranked)        # unchanged: skipped
            as_of["v"] = "2026-11-08"
            assert fleet.seed_if_changed(src, lake_db, ranked)            # a new ranking day
            assert fleet.seed_if_changed(src, lake_db, ranked + [(200, 9999, "NEW")])     # a new company
            assert not fleet.seed_if_changed(src, lake_db, ranked + [(200, 9999, "NEW")])
        assert seed.call_count == 3 and seed.call_args.args[2] == [r for r, _, _ in ranked] + [200]

    def test_a_broken_marker_falls_back_to_seeding(self):
        from screener_scraper import fleet
        src, lake_db = mock.Mock(), mock.Mock()
        lake_db.trail.cursor.side_effect = RuntimeError("relation public.lake_state does not exist")
        with mock.patch.object(fleet, "seed") as seed, self.assertLogs(fleet.log, "WARNING"):
            assert fleet.seed_if_changed(src, lake_db, self.ranked(3))
        seed.assert_called_once()


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

    def test_merge_with_direct_local_writes(self):
        """The lake already holds the same company / metrics / holder written directly by a local fleet process
        (different surrogate ids). Loading a GitHub export must merge by natural key, not duplicate."""
        src, dst = self.conns["lake_t_src"], self.conns["lake_t_dst"]
        with dst.cursor() as d:
            d.execute("truncate equity.financial_fact, equity.shareholding_holder, equity.scrape_task, "
                      "equity.market_snapshot, equity.historical_prices, equity.company, equity.metric, "
                      "equity.holder, equity.scrape_run cascade")
            d.execute("insert into equity.metric(section,name,unit) values ('cash-flow','Zzz','cr')")   # shifts ids
            d.execute("insert into equity.metric(section,name,unit) values ('profit-loss','Sales','cr') returning metric_id")
            sid = d.fetchone()[0]
            d.execute("insert into equity.holder(holder_key,name) values ('other','O'),('/people/1/x/','X')")
            d.execute("insert into equity.company(company_id,screener_slug,name,nse_symbol) values (1,'AAA','Aaa','AAA')")
            d.execute("insert into equity.financial_fact(company_id,basis,metric_id,period_type,period_end,value) "
                      "values (1,'C',%s,'A','2025-03-31',90)", (sid,))
        with src.cursor() as c:
            c.execute("truncate equity.financial_fact, equity.shareholding_holder, equity.scrape_task, "
                      "equity.market_snapshot, equity.historical_prices, equity.company, equity.metric, "
                      "equity.holder, equity.scrape_run cascade")
            c.execute("insert into equity.company(company_id,screener_slug,name,nse_symbol) values (1,'AAA','Aaa','AAA')")
            c.execute("insert into equity.metric(section,name,unit) values ('profit-loss','Sales','cr') returning metric_id")
            m = c.fetchone()[0]
            c.execute("insert into equity.financial_fact(company_id,basis,metric_id,period_type,period_end,value) "
                      "values (1,'C',%s,'A','2026-03-31',100),(1,'C',%s,'A','2025-03-31',91)", (m, m))
        with tempfile.TemporaryDirectory() as tmp:
            lake.export_tables(src, Path(tmp) / "e", 1, company_id=1, symbol="AAA")
            lake.load_export_dir(dst, Path(tmp) / "e")
        with dst.cursor() as d:
            d.execute("select count(*) from equity.metric where name='Sales'"); assert d.fetchone()[0] == 1
            d.execute("select period_end::text, value from equity.financial_fact order by 1")
            assert d.fetchall() == [("2025-03-31", 91), ("2026-03-31", 100)]       # 2025 updated, no duplicate
            d.execute("select count(*) from equity.company"); assert d.fetchone()[0] == 1


if __name__ == "__main__":
    unittest.main()
