"""Pausing the local fleet (scripts/local-fleet.sh pause): a worker finishes its current company, then stops."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from screener_scraper import fleet  # noqa: E402

RANKED = [(101, 1, "AAA"), (102, 2, "BBB"), (103, 3, "CCC")]   # the screener role works them descending


class Pause(unittest.TestCase):
    def setUp(self):
        self.flag = Path(tempfile.mkdtemp()) / "paused"
        p = mock.patch.object(fleet, "PAUSE_FLAG", self.flag)
        p.start()
        self.addCleanup(p.stop)

    def run_pass(self, scrape):
        db = mock.Mock()
        with mock.patch.object(fleet, "source_ranks", return_value=RANKED), \
                mock.patch.object(fleet, "first_rank", return_value=101), mock.patch.object(fleet, "seed_if_changed"), \
                mock.patch.object(fleet, "lake_has_fresh_screener", return_value=False), \
                mock.patch("screener_scraper.pipeline.scrape_company", side_effect=scrape):
            return fleet.run_pass("screener", 0, 1, db, mock.Mock()), db

    def test_a_pause_lands_between_companies(self):
        done = []

        def scrape(sym, run_id, db):
            done.append(sym)
            self.flag.touch()            # paused while CCC is being scraped: CCC finishes, BBB never starts

        st, db = self.run_pass(scrape)
        assert done == ["CCC"] and st["done"] == 1 and st["paused"]
        assert db.finish_run.call_args[0][1] == "partial"

    def test_an_unpaused_pass_runs_every_company(self):
        st, db = self.run_pass(lambda *a, **k: None)
        assert st["done"] == 3 and "paused" not in st and db.finish_run.call_args[0][1] == "success"

    def test_a_paused_worker_exits_instead_of_starting_a_pass(self):
        self.flag.touch()
        with mock.patch.dict(os.environ, {"SOURCE_DATABASE_URL": "postgresql://src"}), \
                mock.patch.object(fleet, "DB") as DB:
            assert fleet.run_fleet_worker("screener", 0, 1) == 0
        DB.assert_not_called()

    def test_the_rest_between_passes_ends_early_on_pause(self):
        self.flag.touch()
        with mock.patch.object(fleet.time, "sleep") as sleep:
            fleet.nap(1800)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class Ranks(unittest.TestCase):
    def test_yf_stops_at_rank_1000_while_the_other_roles_go_on(self):
        ranked = [(1001, 3, "CCC"), (999, 1, "AAA"), (1000, 2, "BBB")]
        self.assertEqual([r[0] for r in fleet.select_for("yf", ranked, 0, 1)], [999, 1000])
        self.assertEqual([r[0] for r in fleet.select_for("bse", ranked, 0, 1)], [999, 1000, 1001])
        self.assertEqual([r[0] for r in fleet.select_for("screener", ranked, 0, 1)], [1001, 1000, 999])
