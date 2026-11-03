"""Console warm loop: it refreshes the slow payloads only while the console is in use (an /api request within
WARM_IDLE_S). Idle, it used to seq-scan the lake's 643 MB financial_fact every 45 s around the clock. No database:
the loaders are mocks, and the TestClient is used without `with`, so the lifespan's warm thread never starts."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from console.api import app as A  # noqa: E402


class WarmOnlyWhileUsed(unittest.TestCase):
    def setUp(self):
        self.loaders = {}
        for name in ("companies", "_load_securities", "doc_catalog", "_load_ops", "_load_rollout"):
            p = mock.patch.object(A, name)
            self.loaders[name] = p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(A.db, "put")
        self.put = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(A, "_last_api_hit", None)
        p.start()
        self.addCleanup(p.stop)

    def test_idle_console_does_no_queries(self):
        assert A._warm_tick(0) == 0
        assert not any(m.called for m in self.loaders.values()) and not self.put.called

    def test_an_api_request_turns_warming_on_for_ten_minutes(self):
        client = TestClient(A.app)
        client.get("/s/TCS", follow_redirects=False)                 # not /api: no activity
        assert A._last_api_hit is None
        client.get("/api/nothing-here")                              # any /api request counts
        assert A._last_api_hit is not None
        hit = A._last_api_hit
        assert A._warm_tick(0) == 1
        self.loaders["_load_securities"].assert_called_once()
        assert self.put.call_args_list[0].args[0] == "securities"
        assert A._last_api_hit == hit                                # warming is not activity: no self-renewal
        A._last_api_hit = time.monotonic() - A.WARM_IDLE_S - 1
        self.loaders["_load_securities"].reset_mock()
        assert A._warm_tick(1) == 1 and not self.loaders["_load_securities"].called


if __name__ == "__main__":
    unittest.main()
