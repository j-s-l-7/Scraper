"""DB reconnect behaviour. The live tests need DATABASE_URL (read-only queries) and are skipped without it."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402

from screener_scraper import db as dbmod  # noqa: E402


def _have_db():
    try:
        from screener_scraper import config
        config.database_url()
        return True
    except Exception:
        return False


class ErrorClassification(unittest.TestCase):
    def test_is_conn_error(self):
        assert dbmod.is_conn_error(psycopg2.InterfaceError("connection already closed"))
        assert dbmod.is_conn_error(psycopg2.OperationalError("server closed the connection unexpectedly"))
        assert dbmod.is_conn_error(psycopg2.OperationalError("SSL connection has been closed unexpectedly"))
        assert not dbmod.is_conn_error(ValueError("nope"))
        e = psycopg2.errors.DiskFull("could not extend file")
        assert not dbmod.is_conn_error(e)             # SQLSTATE 53100: reconnecting does not help


@unittest.skipUnless(_have_db(), "no DATABASE_URL")
class LiveReconnect(unittest.TestCase):
    def setUp(self):
        dbmod.RECONNECT_BACKOFF = (0.1, 0.1, 0.1)      # keep the test fast
        self.db = dbmod.DB()

    def tearDown(self):
        self.db.close()

    def test_trail_method_recovers(self):
        self.db._trail.close()
        assert self.db.company_id_for("TCS") is not None
        assert not self.db._trail.closed

    def test_property_recovers_on_direct_use(self):
        self.db._trail.close()
        with self.db.trail.cursor() as cur:
            cur.execute("select 1")
            assert cur.fetchone() == (1,)

    def test_run_tx_retries_from_start_after_drop(self):
        calls = []

        def fn(cur):
            calls.append(1)
            if len(calls) == 1:
                self.db._conn.close()                  # connection dies mid-transaction
            cur.execute("select 42")
            return cur.fetchone()[0]
        assert self.db.run_tx(fn) == 42 and len(calls) == 2

    def test_keepalives_set(self):
        assert self.db._conn.get_dsn_parameters().get("keepalives") == "1" or "keepalives=1" in self.db._conn.dsn


if __name__ == "__main__":
    unittest.main()
