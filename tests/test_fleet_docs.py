"""Fleet document passes: one scrape_run per pass (not one `docs` run per company: 53,714 rows in 2 days), and document
exceptions counted in the pass stats (passes said failed: 0 next to hundreds of ERROR lines)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from screener_scraper import documents, fleet  # noqa: E402
from screener_scraper.http import FetchError  # noqa: E402

RANKED = [(101, 1, "AAA"), (102, 2, "BBB"), (103, 3, "CCC")]


class DocsPass(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(fleet, "PAUSE_FLAG", Path(tempfile.mkdtemp()) / "paused")
        p.start()
        self.addCleanup(p.stop)

    def run_pass(self, role, docs_cli, retry_due=False):
        db = mock.Mock()
        db.start_run.return_value = 4242
        with mock.patch.object(fleet, "source_ranks", return_value=RANKED), \
                mock.patch.object(fleet, "first_rank", return_value=101), \
                mock.patch.object(fleet, "seed_if_changed"), mock.patch.object(fleet, "has_links", return_value=True), \
                mock.patch.object(fleet, "retry_due", return_value=retry_due), \
                mock.patch.object(documents, "run_docs_cli", side_effect=docs_cli) as cli:
            return fleet.run_pass(role, 0, 1, db, mock.Mock()), db, cli

    def test_document_work_records_into_the_pass_run(self):
        st, db, cli = self.run_pass("concalls", lambda *a, **k: 0, retry_due=True)
        db.start_run.assert_called_once()                       # the pass's own run, nothing per company
        assert cli.call_count == 6 and all(c.kwargs["run_id"] == 4242 for c in cli.call_args_list)
        assert st["done"] == 3 and st["doc_errors"] == 0 and db.finish_run.call_args[0][1] == "success"

    def test_document_exceptions_are_counted(self):
        def cli(db, syms, *a, totals=None, **k):
            if syms == ["BBB"]:
                totals["doc_errors"] = totals.get("doc_errors", 0) + 2
            return 0
        st, db, _ = self.run_pass("nse", cli)
        assert st["doc_errors"] == 2 and st["done"] == 3 and st["failed"] == 0
        assert db.finish_run.call_args[0][1] == "partial" and db.finish_run.call_args[0][2]["doc_errors"] == 2


class RunDocsCli(unittest.TestCase):
    def db(self):
        db = mock.MagicMock()
        db.trail.cursor.return_value.__enter__.return_value.fetchone.return_value = (7, "AAA")
        return db

    def test_a_caller_run_id_is_reused_and_errors_reach_its_totals(self):
        db, tot = self.db(), {}
        with mock.patch.object(documents, "run_company", side_effect=FetchError("HTTP 503", 503, 3)) as rc:
            assert documents.run_docs_cli(db, ["AAA"], ["annual_report"], run_id=99, totals=tot) == 0
        assert rc.call_args[0][2] == 99 and tot["doc_errors"] == 1
        db.start_run.assert_not_called()
        db.finish_run.assert_not_called()

    def test_without_a_run_id_the_cli_keeps_its_own_run(self):
        db = self.db()
        db.start_run.return_value = 5
        with mock.patch.object(documents, "run_company", return_value={"docs": [], "ok": 0, "no_text": 0, "missing": 0,
                                                                       "failed": 0, "skipped": 0, "pages": 0,
                                                                       "chars": 0, "seconds": 0}) as rc:
            assert documents.run_docs_cli(db, ["AAA"], ["annual_report"]) == 0
        assert rc.call_args[0][2] == 5 and db.finish_run.call_args[0][:2] == (5, "success")

    def test_retry_missing_errors_are_doc_errors(self):
        db, tot = self.db(), {}
        with mock.patch("screener_scraper.fallback.retry_company", side_effect=RuntimeError("boom")):
            documents.run_docs_cli(db, ["AAA"], ["concall_transcript"], retry_missing=True, run_id=99, totals=tot)
        assert tot["doc_errors"] == 1


if __name__ == "__main__":
    unittest.main()
