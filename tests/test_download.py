"""Document downloads: the overall time cap and the per-host breaker. No network: sessions are patched."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from screener_scraper import documents as D  # noqa: E402
from screener_scraper import fallback as F  # noqa: E402
from screener_scraper.http import FetchError  # noqa: E402


class NoWait:
    def wait(self):
        pass

    def on_ok(self):
        pass

    def on_429(self):
        pass


class Answer:
    """An HTTP response; endless=True never finishes its body (a server trickling bytes)."""
    def __init__(self, status=200, endless=False):
        self.status_code, self.endless, self.headers = status, endless, {"Content-Type": "application/pdf"}

    def iter_content(self, n):
        while self.endless:
            yield b"x" * 10
        yield b"%PDF-1.4 tiny"

    def close(self):
        pass


class Session:
    def __init__(self, answer=None):
        self.answer, self.calls = answer, 0

    def get(self, url, **kw):
        self.calls += 1
        if self.answer is None:
            raise requests.ConnectionError("connect timeout")
        return self.answer


class Download(unittest.TestCase):
    def setUp(self):
        D._breaker.clear()
        patcher = mock.patch("time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dead_host_skipped_after_three_failed_downloads(self):
        dead = Session()
        with mock.patch.object(D, "_session", return_value=dead):
            for i in range(3):
                with self.assertRaises(FetchError):
                    D.download(f"https://ir.dead.example/{i}.pdf", NoWait(), tries=2)
            self.assertEqual(dead.calls, 6)                       # 3 downloads x 2 attempts
            with self.assertRaises(FetchError) as cm:
                D.download("https://ir.dead.example/3.pdf", NoWait(), tries=2)
            self.assertEqual(dead.calls, 6)                       # the 4th made no request
            self.assertIn("skipped", str(cm.exception))
            with self.assertRaises(FetchError):
                D.download("https://other.example/a.pdf", NoWait(), tries=1)
            self.assertEqual(dead.calls, 7)                       # other hosts are still tried

    def test_any_http_answer_resets_the_host(self):
        D._breaker["ir.flaky.example"] = (2, 0.0)
        with mock.patch.object(D, "_session", return_value=Session(Answer(404))):
            with self.assertRaises(D.DocNotFound):
                D.download("https://ir.flaky.example/a.pdf", NoWait())
        self.assertNotIn("ir.flaky.example", D._breaker)
        with mock.patch.object(D, "_session", return_value=Session(Answer(200))):
            self.assertTrue(D.download("https://ir.flaky.example/b.pdf", NoWait()).startswith(b"%PDF"))

    def test_endless_body_hits_the_time_cap_without_retry(self):
        s = Session(Answer(200, endless=True))
        with mock.patch.object(D, "_session", return_value=s), mock.patch.object(D, "MAX_GET_S", -1):
            with self.assertRaises(FetchError) as cm:
                D.download("https://slow.example/a.pdf", NoWait(), tries=3)
        self.assertEqual(s.calls, 1)                              # not retried
        self.assertIn("still downloading", str(cm.exception))
        self.assertEqual(D._breaker["slow.example"][0], 1)        # counts toward the breaker

    def test_fallback_get_time_cap_without_retry(self):
        po = F.Polite()
        po.s, po.gap = Session(Answer(200, endless=True)), 0
        with mock.patch.object(F, "MAX_GET_S", -1):
            with self.assertRaises(TimeoutError):
                po.get("https://slow.example/page")
        self.assertEqual(po.s.calls, 1)


if __name__ == "__main__":
    unittest.main()
