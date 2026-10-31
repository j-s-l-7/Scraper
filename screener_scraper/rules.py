"""Rule engine: exception -> error_class -> action (ARCHITECTURE.md section 8, rules R1-R11).

Pure functions, no I/O: `classify(exc)` returns an error class, `decide(exc, ...)` returns a Decision that
jobs.fail() applies. R10 (stale reclaim) and R8's storage check live in the supervisor / jobs module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# error classes
RATE_LIMITED, BLOCKED, NETWORK, SERVER = "rate_limited", "blocked", "network", "server"
NOT_FOUND, PARSE, DB_TRANSIENT, DB_FULL = "not_found", "parse", "db_transient", "db_full"
PDF_NO_TEXT, STALE, UNKNOWN = "pdf_no_text", "stale", "unknown"

RULE_OF = {RATE_LIMITED: "R1", BLOCKED: "R2", NETWORK: "R3", SERVER: "R4", NOT_FOUND: "R5", PARSE: "R6",
           DB_TRANSIENT: "R7", DB_FULL: "R8", PDF_NO_TEXT: "R9", STALE: "R10", UNKNOWN: "R11"}

BLOCK_RE = re.compile(r"captcha|access denied|cloudflare|attention required|just a moment|are you a robot", re.I)
DB_FULL_RE = re.compile(r"read-only transaction|database is read-only|could not extend file|disk full|no space left|"
                        r"project is over|exceeded .* quota|53100", re.I)


class NoTextError(Exception):
    """A PDF with no extractable text (encrypted, scanned, empty)."""


class DocNotFound(Exception):
    """A 404 on a document url (R5: only that document is 'missing')."""


@dataclass
class Decision:
    error_class: str
    rule: str
    action: str                       # retry | dead | pause
    delay_s: int = 0                  # for retry: next_run_at = now() + delay
    route: Optional[str] = None       # pin the job: 'local' | 'gh' | None = unchanged
    force_fresh: bool = False         # R6: next attempt must bypass the raw cache
    interval_x2: bool = False         # R1: the worker doubles its request interval
    controls: dict = field(default_factory=dict)   # control rows to set: {key: (value, reason)}
    note: str = ""


def http_status_of(exc: BaseException) -> Optional[int]:
    st = getattr(exc, "status", None)
    if st is None:
        st = getattr(exc, "status_code", None)
    if st is None:
        resp = getattr(exc, "response", None)
        st = getattr(resp, "status_code", None)
    try:
        return int(st) if st is not None else None
    except (TypeError, ValueError):
        return None


def sqlstate_of(exc: BaseException) -> Optional[str]:
    return getattr(exc, "pgcode", None)


def classify(exc: BaseException) -> str:
    # lazy imports keep the rule engine importable without the heavy deps
    try:
        import psycopg2
    except Exception:                                      # pragma: no cover
        psycopg2 = None
    try:
        import requests
    except Exception:                                      # pragma: no cover
        requests = None
    msg = str(exc)
    if isinstance(exc, NoTextError):
        return PDF_NO_TEXT
    if isinstance(exc, DocNotFound):
        return NOT_FOUND
    if psycopg2 is not None and isinstance(exc, psycopg2.Error):
        if sqlstate_of(exc) == "53100" or DB_FULL_RE.search(msg):
            return DB_FULL
        if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
            return DB_TRANSIENT
        return UNKNOWN
    if DB_FULL_RE.search(msg) and "http" not in msg.lower():
        return DB_FULL
    st = http_status_of(exc)
    name = type(exc).__name__
    if name == "ParseError" and re.search(r"block|captcha|login|log in|sign in|access denied|forbidden", msg, re.I):
        return BLOCKED
    if name in ("FetchError", "HTTPFailure") and st is None:
        return NETWORK
    if st == 429:
        return RATE_LIMITED
    if st == 403 or (st is not None and st < 500 and BLOCK_RE.search(msg)) or \
            (st is None and BLOCK_RE.search(msg) and not isinstance(exc, KeyError)):
        return BLOCKED
    if st == 404:
        return NOT_FOUND
    if st is not None and 500 <= st < 600:
        return SERVER
    if st is None and type(exc).__name__ == "ParseError":
        return PARSE
    if isinstance(exc, (TimeoutError, ConnectionError)) or \
            (requests is not None and isinstance(exc, (requests.Timeout, requests.ConnectionError))):
        return NETWORK
    if re.search(r"timed? ?out|connection (reset|aborted|refused)|name resolution|temporary failure in name|"
                 r"nodename nor servname|connection error|max retries exceeded", msg, re.I):
        return NETWORK
    if type(exc).__name__ in ("ParseError", "KeyError", "AssertionError") and _in_parse(exc):
        return PARSE
    return UNKNOWN


def _in_parse(exc: BaseException) -> bool:
    """KeyError / AssertionError count as parse errors only when raised inside parse code."""
    tb = exc.__traceback__
    while tb is not None:
        fn = tb.tb_frame.f_code.co_filename
        if fn.endswith("parse.py") or fn.endswith("parse"):
            return True
        tb = tb.tb_next
    return type(exc).__name__ == "ParseError"


def history_count(history, error_class: str) -> int:
    return sum(1 for h in (history or []) if h.get("error_class") == error_class)


def decide(exc: BaseException, *, attempts: int, max_attempts: int = 6, worker_class: str = "local",
           history=None, kind: str = "screener") -> Decision:
    """Pick the action for a failed job. `attempts` already includes the failed attempt (claim increments it)."""
    cls = classify(exc)
    a = max(attempts, 1)
    rule = RULE_OF[cls]
    d: Decision

    if cls == RATE_LIMITED:                                               # R1
        d = Decision(cls, rule, "retry", delay_s=600 * a, interval_x2=True, note="429: interval x2, retry 10min*attempt")
    elif cls == BLOCKED:                                                  # R2
        if worker_class == "gh":
            d = Decision(cls, rule, "retry", delay_s=0, route="local", note="blocked on gh: pin to local, retry now")
        else:
            d = Decision(cls, rule, "retry", delay_s=1800, note="blocked on local: retry in 30min")
            if history_count(history, BLOCKED) + 1 >= 2:
                d.controls["screener_min_interval"] = (4.0, "2 blocks: slow down screener to 4s")
                d.note += "; screener_min_interval=4s"
    elif cls == NETWORK:                                                  # R3
        d = Decision(cls, rule, "retry", delay_s=120 * 2 ** a, note="network: retry 2min*2^attempt")
    elif cls == SERVER:                                                   # R4
        d = Decision(cls, rule, "retry", delay_s=300 * a, note="5xx: retry 5min*attempt")
    elif cls == NOT_FOUND:                                                # R5
        d = Decision(cls, rule, "dead", note="404: dead (documents are marked missing inside the job)")
    elif cls == PARSE:                                                    # R6
        if history_count(history, PARSE) == 0:
            d = Decision(cls, rule, "retry", delay_s=0, force_fresh=True, note="parse: retry once with a fresh fetch")
        else:
            d = Decision(cls, rule, "dead", note="parse failed twice: dead, raw path kept for diagnosis")
    elif cls == DB_TRANSIENT:                                             # R7
        d = Decision(cls, rule, "retry", delay_s=60, note="db transient: reconnect, retry in 1min")
    elif cls == DB_FULL:                                                  # R8
        d = Decision(cls, rule, "pause", delay_s=600, controls={"paused": (True, f"db_full: {str(exc)[:200]}")},
                     note="db full: control.paused=true")
    elif cls == PDF_NO_TEXT:                                              # R9 (normally handled per document)
        d = Decision(cls, rule, "retry", delay_s=0, note="pdf without text: document marked no_text, job continues")
    else:                                                                 # R11
        d = Decision(cls, rule, "retry", delay_s=900 * a, note="unknown: retry 15min*attempt")

    # a job past max_attempts is dead for the rest of the batch (db_full never kills a job: it pauses)
    if d.action == "retry" and cls not in (DB_FULL, PDF_NO_TEXT) and a >= max_attempts:
        d.action = "dead"
        d.note += f"; attempts {a}/{max_attempts} exhausted"
    return d
