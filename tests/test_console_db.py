"""Console DB layer: a dead pooled connection (bare DatabaseError from set_session) is discarded, the pool reset
and the query retried; SQL errors such as a statement timeout raise at once. No database: fake pool/connections."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402
import psycopg2.errors  # noqa: E402

from console.api import db  # noqa: E402


class FakeCur:
    def __init__(self, conn):
        self.c = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if not sql.startswith("set "):
            self.c.executed += 1
            if self.c.exec_err:
                raise self.c.exec_err

    def fetchall(self):
        return [{"x": 1}]


class FakeConn:
    closed = 0

    def __init__(self, set_err=None, exec_err=None):
        self.set_err, self.exec_err, self.executed = set_err, exec_err, 0

    def set_session(self, **kw):
        if self.set_err:
            raise self.set_err

    def cursor(self, **kw):
        return FakeCur(self)


class FakePool:
    def __init__(self, conns):
        self.conns, self.put = list(conns), []

    def getconn(self):
        return self.conns.pop(0)

    def putconn(self, conn, close=False):
        self.put.append((conn, close))


class ConsoleDb(unittest.TestCase):
    def setUp(self):
        for name in ("time.sleep",):
            p = mock.patch(name)
            p.start()
            self.addCleanup(p.stop)

    def test_dead_pooled_connection_is_dropped_pool_reset_and_retried(self):
        dead = FakeConn(set_err=psycopg2.DatabaseError("server closed the connection unexpectedly\n"))
        good = FakeConn()
        pool = FakePool([dead, good])
        with mock.patch.object(db, "_get_pool", return_value=pool), mock.patch.object(db, "reset_pool") as rp:
            rows = db.query("select 1", target="supabase")
        assert rows == [{"x": 1}]
        assert pool.put[0] == (dead, True) and pool.put[1] == (good, False)
        rp.assert_called_once_with("supabase")

    def test_statement_timeout_is_not_retried(self):
        c = FakeConn(exec_err=psycopg2.errors.QueryCanceled("canceling statement due to statement timeout"))
        pool = FakePool([c, FakeConn()])
        with mock.patch.object(db, "_get_pool", return_value=pool), mock.patch.object(db, "reset_pool") as rp:
            with self.assertRaises(psycopg2.errors.QueryCanceled):
                db.query("select 1", target="supabase")
        assert c.executed == 1 and len(pool.put) == 1 and pool.put[0] == (c, False)
        rp.assert_not_called()

    def test_timeout_ms_is_applied(self):
        seen = []
        c = FakeConn()
        orig = FakeCur.execute
        with mock.patch.object(FakeCur, "execute", lambda s, q, p=None: (seen.append(q), orig(s, q, p))):
            with mock.patch.object(db, "_get_pool", return_value=FakePool([c])):
                db.query("select 1", target="lake", timeout_ms=180000)
        assert "set statement_timeout = 180000" in seen


if __name__ == "__main__":
    unittest.main()
