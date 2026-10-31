"""Single polite HTTP session with retry/backoff and a gzip raw cache.

Cache layout: data/raw/<YYYY-MM-DD>/<slug>/<name>.gz  (body text, gzip).
A cache hit returns without any network traffic.
"""
from __future__ import annotations

import gzip
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import requests

from . import config

log = logging.getLogger(__name__)


@dataclass
class Response:
    text: str
    status: int
    attempts: int
    from_cache: bool
    path: Optional[Path]


class HTTPFailure(requests.HTTPError):
    """HTTP-level failure carrying `.status_code` (429/403/404/5xx) so the rule engine can classify it."""

    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status_code = status


class FetchError(HTTPFailure):
    def __init__(self, msg: str, status: Optional[int] = None, attempts: int = 0):
        super().__init__(msg, status)
        self.status = status
        self.attempts = attempts


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:150]


class RateLimiter:
    """One global request-rate limiter shared by all threads, with adaptive throttling.

    acquire() hands out request slots at least `interval` (+ random jitter) apart across ALL threads, so extra
    worker threads only overlap network latency; they never exceed the rate. A 429 doubles the interval (cap
    `max_interval`); after `decay_after` consecutive successes it decays one step (x0.8) back toward the base.
    """

    def __init__(self, min_interval: float, jitter: float, max_interval: float = 10.0, decay_after: int = 20,
                 clock=time.monotonic, sleep=time.sleep, rand=random.uniform):
        self.base = min_interval
        self.interval = min_interval
        self.jitter = jitter
        self.max_interval = max_interval
        self.decay_after = decay_after
        self.ok_streak = 0
        self.next_at = 0.0
        self._lock = threading.Lock()
        self._clock, self._sleep, self._rand = clock, sleep, rand

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self.next_at)
            self.next_at = start + self.interval + self._rand(0, self.jitter)
        if start > now:
            self._sleep(start - now)

    def on_429(self) -> None:
        with self._lock:
            self.ok_streak = 0
            new = min(self.interval * 2, self.max_interval)
            if new != self.interval:
                log.warning("rate limited: request interval %.1fs -> %.1fs", self.interval, new)
            self.interval = new

    def on_success(self) -> None:
        with self._lock:
            self.ok_streak += 1
            if self.ok_streak >= self.decay_after:
                self.ok_streak = 0
                if self.interval > self.base:
                    self.interval = max(self.base, self.interval * 0.8)


class Client:
    def __init__(self, cache_date: Optional[date] = None, use_cache: bool = True, offline: bool = False):
        self._local = threading.local()        # one Session per thread; the rate limiter is shared
        self.limiter = RateLimiter(config.MIN_INTERVAL, config.JITTER, config.MAX_INTERVAL, config.DECAY_AFTER)
        self._count_lock = threading.Lock()
        self.cache_date = cache_date or date.today()
        self.use_cache = use_cache
        self.offline = offline      # reparse mode: cache only, never touch the network
        self.requests_made = 0
        self.cache_hits = 0

    @property
    def s(self) -> requests.Session:
        sess = getattr(self._local, "s", None)
        if sess is None:
            sess = self._local.s = requests.Session()
            sess.headers.update({
                "User-Agent": config.USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            })
        return sess

    def _count(self, hit: bool = False) -> None:
        with self._count_lock:
            if hit:
                self.cache_hits += 1
            else:
                self.requests_made += 1

    def cache_path(self, slug: str, name: str, day: Optional[date] = None) -> Path:
        d = (day or self.cache_date).isoformat()
        return config.RAW_DIR / d / safe_name(slug) / (safe_name(name) + ".gz")

    def get(self, url: str, slug: str, name: str, *, headers: Optional[dict] = None,
            params=None, ok_statuses=(200,), use_cache: Optional[bool] = None) -> Response:
        """GET with cache. Raises FetchError after retries are exhausted."""
        path = self.cache_path(slug, name)
        if (self.use_cache if use_cache is None else use_cache) and path.exists():
            self._count(hit=True)
            return Response(gzip.decompress(path.read_bytes()).decode("utf-8"), 200, 0, True, path)

        if self.offline:
            raise FetchError(f"cache miss: {path}", None, 0)

        last_err, status = "", None
        for attempt in range(1, config.MAX_ATTEMPTS + 1):
            self.limiter.acquire()
            retry_after = None
            try:
                r = self.s.get(url, headers=headers, params=params, timeout=config.TIMEOUT)
                self._count()
                status = r.status_code
                if status in ok_statuses:
                    self.limiter.on_success()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(gzip.compress(r.content))
                    return Response(r.text, status, attempt, False, path)
                last_err = f"HTTP {status}"
                if status == 429 or status >= 500:
                    if status == 429:
                        self.limiter.on_429()
                    ra = r.headers.get("Retry-After")
                    if ra and ra.isdigit():
                        retry_after = float(ra)
                else:
                    raise FetchError(f"HTTP {status} for {url}", status, attempt)  # 4xx: not retryable
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = f"{type(e).__name__}: {e}"
                self._count()
            if attempt < config.MAX_ATTEMPTS:
                delay = retry_after if retry_after is not None else config.BACKOFF_BASE * 2 ** (attempt - 1)
                delay += random.uniform(0, 1)
                log.warning("retry %d/%d for %s (%s) in %.1fs", attempt, config.MAX_ATTEMPTS, url, last_err, delay)
                time.sleep(min(delay, 120))
        raise FetchError(f"{last_err} for {url}", status, config.MAX_ATTEMPTS)
