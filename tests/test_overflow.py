"""Supabase overflow (R8): the db_full latch, the command gate, and the lake becoming primary for the top 100.
No database: connections are fakes scripted by SQL substring."""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402

from screener_scraper import __main__ as M  # noqa: E402
from screener_scraper import fleet, jobs, lake, quality as Q  # noqa: E402
from screener_scraper import lake_drive as LD  # noqa: E402

SUPABASE = "postgresql://u@aws-0-ap-south-1.pooler.supabase.com:6543/postgres"
LAKE = "postgresql://localhost:5433/equity_lake"


class Conn:
    """{sql substring: rows}; statements containing a `fail` substring raise like a read-only database."""
    def __init__(self, rows=None, fail=()):
        self.rows, self.fail, self.sql, self.autocommit = rows or {}, fail, [], True

    def cursor(self, *a, **k):
        return Cur(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def set_session(self, **k):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class Cur:
    def __init__(self, c):
        self.c, self.out = c, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, p=()):
        self.c.sql.append(q)
        if any(s in q for s in self.c.fail):
            raise RuntimeError("cannot execute INSERT in a read-only transaction")
        self.out = next((list(v) for k, v in self.c.rows.items() if k in q), [])

    def fetchone(self):
        return self.out[0] if self.out else None

    def fetchall(self):
        return self.out


def supabase(size=454.4, paused=(False, "initial"), fail=()):
    return Conn({"key='paused'": [paused], "where key=%s": [(470,)], "pg_database_size": [(size,)]}, fail)


def writes(c):
    return [q for q in c.sql if q.lstrip().startswith("insert")]


class OverflowLatch(unittest.TestCase):
    def test_a_latched_reason_is_returned_without_measuring(self):
        c = supabase(paused=(True, "db_full: 471 MB >= limit 470 MB (R8)"))
        assert jobs.overflow_reason(c) == "db_full: 471 MB >= limit 470 MB (R8)"
        assert not writes(c) and not any("pg_database_size" in q for q in c.sql)

    def test_under_the_limit_is_not_overflow(self):
        c = supabase(size=454.4)
        assert jobs.overflow_reason(c) is None and not writes(c)

    def test_reaching_the_limit_latches(self):
        c = supabase(size=470.4)
        assert jobs.overflow_reason(c) == "db_full: 470 MB >= limit 470 MB (R8)" and len(writes(c)) == 1
        c = supabase(size=470.4)
        assert jobs.overflow_reason(c, latch=False).startswith("db_full") and not writes(c)

    def test_a_manual_pause_is_not_overflow(self):
        assert jobs.overflow_reason(supabase(paused=(True, "manual: maintenance"))) is None

    def test_a_read_only_database_is_still_overflow(self):
        c = supabase(size=499, fail=("insert into equity.control",))
        assert jobs.overflow_reason(c) == "db_full: 499 MB >= limit 470 MB (R8)"

    def test_only_supabase_urls_are_checked(self):
        with mock.patch("psycopg2.connect", side_effect=AssertionError("connected")):
            assert jobs.overflow_for_url(LAKE) is None and jobs.overflow_for_url(None) is None


class CommandGate(unittest.TestCase):
    def test_only_writers_into_a_full_supabase_are_skipped(self):
        full = mock.Mock(url=SUPABASE, trail=supabase(size=480))
        assert M.overflow_gate("scrape", full).startswith("db_full")
        assert M.overflow_gate("coverage", full) is None                     # reads only
        assert M.overflow_gate("universe", full) is None                     # identity rows stay allowed
        assert M.overflow_gate("worker", mock.Mock(url=SUPABASE, trail=supabase())) is None
        assert M.overflow_gate("scrape", mock.Mock(url=LAKE, trail=supabase(size=9999))) is None   # never full


class Top100Plan(unittest.TestCase):
    def test_plan(self):
        sat, mon = date(2026, 11, 7), date(2026, 11, 2)
        full = "db_full: 471 MB >= limit 470 MB (R8)"
        everything = tuple(lake.TABLES)
        assert LD.plan_top100(None, False, False, sat, None, None, None) == (everything, True, True, False)
        assert LD.plan_top100(None, False, False, mon, None, None, None) == (everything, True, False, False)
        # full: identity rows only, no GitHub writers, the lake becomes primary
        assert LD.plan_top100(full, False, False, sat, None, None, None) == (LD.IDENTITY, False, False, True)
        # pause cleared but the lake still primary: identity rows only, writers allowed again
        assert LD.plan_top100(None, False, True, sat, None, None, None) == (LD.IDENTITY, True, True, False)
        # Supabase unreachable: no sync, no dispatch, primary left alone
        assert LD.plan_top100(None, True, False, sat, None, None, None) == (None, False, False, False)
        assert LD.plan_top100(None, False, False, mon, mon.isoformat(), mon.isoformat(), None)[:2] == (None, False)


class LakePrimary(unittest.TestCase):
    def test_the_fleet_takes_the_top_100_once_the_lake_is_primary(self):
        assert fleet.first_rank(mock.Mock(trail=Conn({"top100_primary": [(1,)]}))) == 1
        assert fleet.first_rank(mock.Mock(trail=Conn())) == fleet.FIRST_RANK == 101

    def test_dq_skips_a_full_supabase_but_runs_the_lake(self):
        db = mock.Mock(url=SUPABASE, trail=supabase(size=480))
        with mock.patch("screener_scraper.db.DB", return_value=db), mock.patch.object(Q, "run_dq") as run_dq, \
                mock.patch.object(Q, "_lake_part", return_value={"db": "lake"}) as lake_part, \
                mock.patch("builtins.print"):
            assert Q.dq_cli("both") == 0
        run_dq.assert_not_called()
        lake_part.assert_called_once()

    def test_library_reads_the_lakes_top_100_once_the_lake_is_primary(self):
        from library import index as I
        cat = Conn()
        with mock.patch.object(lake, "lake_connect", return_value=Conn({"top100_primary": [(1,)]})), \
                mock.patch("psycopg2.connect", return_value=cat) as connect:
            assert I.catalog() == []
        assert connect.call_args[0][0] == lake.lake_url() and "TOP100_MCAP" in cat.sql[0]

    def test_library_never_falls_back_to_supabase_when_the_lake_is_down(self):
        from library import index as I
        with mock.patch.object(lake, "lake_connect", side_effect=psycopg2.OperationalError("down")), \
                mock.patch("psycopg2.connect", side_effect=AssertionError("fell back to Supabase")):
            with self.assertRaises(psycopg2.OperationalError):
                I.catalog()


if __name__ == "__main__":
    unittest.main()
