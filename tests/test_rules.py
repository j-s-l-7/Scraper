import psycopg2
import pytest
import requests

from screener_scraper import rules
from screener_scraper.http import FetchError
from screener_scraper.parse import ParseError


def D(exc, attempts=1, cls="local", history=None, max_attempts=6):
    return rules.decide(exc, attempts=attempts, max_attempts=max_attempts, worker_class=cls, history=history)


def test_r1_rate_limited():
    d = D(FetchError("HTTP 429", 429), attempts=2)
    assert (d.error_class, d.rule, d.action) == ("rate_limited", "R1", "retry")
    assert d.delay_s == 1200 and d.interval_x2


def test_r2_blocked_gh_pins_local_and_retries_now():
    d = D(FetchError("HTTP 403", 403), cls="gh")
    assert d.error_class == "blocked" and d.route == "local" and d.delay_s == 0


def test_r2_blocked_local_30min_and_slow_after_two():
    d = D(FetchError("HTTP 403", 403))
    assert d.delay_s == 1800 and not d.controls
    d2 = D(FetchError("HTTP 403", 403), history=[{"error_class": "blocked"}])
    assert d2.controls["screener_min_interval"][0] == 4.0


@pytest.mark.parametrize("msg", ["captcha required", "Access Denied", "no data-company-id: blocked page"])
def test_r2_block_detected_from_parse_error_or_html(msg):
    assert rules.classify(ParseError(msg)) == "blocked"
    assert rules.classify(RuntimeError(msg)) == "blocked" or "company-id" in msg


def test_r3_network():
    for e in (requests.ConnectionError("reset"), requests.Timeout("t"), FetchError("dns", None)):
        assert rules.classify(e) == "network"
    d = D(requests.Timeout("t"), attempts=2)
    assert d.rule == "R3" and d.delay_s == 120 * 4


def test_r4_server():
    d = D(FetchError("HTTP 503", 503), attempts=3)
    assert (d.error_class, d.delay_s) == ("server", 900)


def test_r5_not_found_dead():
    assert D(FetchError("HTTP 404", 404)).action == "dead"
    assert rules.classify(rules.DocNotFound("x")) == "not_found"


def test_r6_parse_retry_once_then_dead():
    d = D(ParseError("bad table"))
    assert d.error_class == "parse" and d.action == "retry" and d.force_fresh
    d2 = D(ParseError("bad table"), history=[{"error_class": "parse"}])
    assert d2.action == "dead"


def test_r7_db_transient():
    d = D(psycopg2.OperationalError("server closed the connection unexpectedly"))
    assert (d.error_class, d.delay_s, d.action) == ("db_transient", 60, "retry")


def test_r8_db_full_pauses():
    e = psycopg2.errors.DiskFull("could not extend file")
    d = D(e)
    assert d.error_class == "db_full" and d.action == "pause" and d.controls["paused"][0] is True
    assert rules.classify(psycopg2.errors.ReadOnlySqlTransaction("cannot execute INSERT in a read-only transaction")) == "db_full"


def test_r9_no_text():
    assert D(rules.NoTextError("scanned")).error_class == "pdf_no_text"


def test_r11_unknown_and_exhaustion():
    d = D(ValueError("weird"), attempts=2)
    assert (d.error_class, d.delay_s, d.action) == ("unknown", 1800, "retry")
    assert D(ValueError("weird"), attempts=6).action == "dead"
    assert D(FetchError("HTTP 503", 503), attempts=6).action == "dead"


def test_keyerror_outside_parse_is_unknown():
    assert rules.classify(KeyError("x")) == "unknown"
