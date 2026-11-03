"""Ops sections load independently: a failing query lands in `errors` and the last good value is served stale.
No database: db.query is mocked."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from console.api import app as A  # noqa: E402


def fake_query(fail=()):
    def q(sql, params=None, **kw):
        if any(f in sql for f in fail):
            raise RuntimeError("boom")
        return []
    return q


class OpsSections(unittest.TestCase):
    def setUp(self):
        A._last_good.clear()
        self.addCleanup(A._last_good.clear)

    def test_one_failed_section_does_not_blank_the_rest(self):
        with mock.patch.object(A.db, "query", side_effect=fake_query(fail=("equity.job group",))):
            out = A._load_ops()
        assert set(out["errors"]) == {"ops_queue", "ops_storage"}   # storage reads size from a mocked empty result
        assert out["runs"] == [] and out["documents"] == [] and out["completeness"]["companies"] == 0
        assert "jobs" not in out

    def test_failed_section_serves_last_good_value(self):
        with mock.patch.object(A.db, "query", side_effect=fake_query()):
            A._load_ops()
        A._last_good["ops_queue"] = {"jobs": [{"kind": "k", "status": "done", "n": 1}]}
        with mock.patch.object(A.db, "query", side_effect=fake_query(fail=("equity.job group",))):
            out = A._load_ops()
        assert out["jobs"][0]["kind"] == "k" and "ops_queue" in out["errors"]

    def test_rollout_sections_isolated_and_dq_delta(self):
        dq = [{"as_of": "2026-11-03", "companies": 10, "avg_overall": 60.0, "open_errors": 5, "open_warns": 2, "resolved": 3,
               "by_check": {"a": 1, "b": 4}}, {"as_of": "2026-11-02", "companies": 8, "avg_overall": 70.0, "open_errors": 2,
                                                "open_warns": 2, "resolved": 1, "by_check": {}}]

        def q(sql, params=None, **kw):
            if "_COVERAGE" in sql or "TOP1000_MCAP" in sql:
                raise RuntimeError("slow")
            if "dq_daily" in sql:
                return dq
            return []
        with mock.patch.object(A.db, "query", side_effect=q), mock.patch.object(A.db, "one", return_value=None):
            out = A._load_rollout()
        assert list(out["errors"]) == ["rollout_bands"] and out["runs"] == [] and out["top100_primary"] is None
        assert out["dq"]["top_checks"][0] == ("b", 4)
        assert out["dq"]["delta"] == {"companies": 2}       # the universe grew: no like-for-like score/error delta
        dq[1]["companies"] = 10
        A._last_good.clear()
        with mock.patch.object(A.db, "query", side_effect=q), mock.patch.object(A.db, "one", return_value=None):
            out = A._load_rollout()
        assert out["dq"]["delta"]["open_errors"] == 3 and out["dq"]["delta"]["avg_overall"] == -10.0

    def test_rollout_bands_extend_past_1000_to_max_rank(self):
        cov = [{"band": b, "total": 3, "hi": h, "screener": 1, "yf": 1, "ar": 0, "cc": 0}
               for b, h in ((1, 100), (1001, 1300), (1301, 1600), (1901, 1915))]
        with mock.patch.object(A.db, "query", side_effect=lambda sql, *a, **k: cov if "TOP1000_MCAP" in sql else []), \
                mock.patch.object(A.db, "one", return_value=None):
            bands = A._load_rollout()["bands"]
        assert [(b["from"], b["to"]) for b in bands] == [(1, 100), (101, 400), (401, 700), (701, 1000), (1001, 1300), (1301, 1600), (1901, 1915)]


if __name__ == "__main__":
    unittest.main()
