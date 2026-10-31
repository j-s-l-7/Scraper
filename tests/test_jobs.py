"""Queue tests against the real DB, using a throwaway batch 'test-<random>' that is deleted afterwards."""
import json
import uuid

import psycopg2
import pytest

from screener_scraper import config, jobs, rules
from screener_scraper.http import FetchError


@pytest.fixture()
def conn():
    try:
        c = psycopg2.connect(config.database_url(), connect_timeout=15)
    except Exception as e:                      # pragma: no cover
        pytest.skip(f"no database: {e}")
    c.autocommit = True
    yield c
    c.close()


@pytest.fixture()
def batch(conn):
    b = "test-" + uuid.uuid4().hex[:8]
    yield b
    with conn.cursor() as cur:
        cur.execute("delete from equity.job where batch=%s", (b,))


def companies(conn):
    with conn.cursor() as cur:
        cur.execute("""select c.company_id, exists(select 1 from equity.document_link d where d.company_id=c.company_id)
                       from equity.company c order by 2 desc, 1""")
        return cur.fetchall()


def pick(conn):
    rows = companies(conn)
    with_links = [r[0] for r in rows if r[1]]
    without = [r[0] for r in rows if not r[1]]
    return with_links, without


def status(conn, batch, kind, cid):
    with conn.cursor() as cur:
        cur.execute("select status, attempts, route, history from equity.job where batch=%s and kind=%s and company_id=%s",
                    (batch, kind, cid))
        return cur.fetchone()


def test_claim_complete_and_skip_locked(conn, batch):
    with_links, without = pick(conn)
    cid = (with_links or without)[0]
    assert jobs.enqueue_companies(conn, batch, [cid], ["yf"]) == 1
    assert jobs.enqueue_companies(conn, batch, [cid], ["yf"]) == 0          # idempotent
    j = jobs.claim(conn, "t:1:0", ["yf"], "local", batch)
    assert j and j["status"] == "running" and j["attempts"] == 1 and j["locked_by"] == "t:1:0"
    assert jobs.claim(conn, "t:1:1", ["yf"], "local", batch) is None           # nothing else claimable
    jobs.complete(conn, j["job_id"])
    assert status(conn, batch, "yf", cid)[0] == "done"


def test_route_and_next_run_at(conn, batch):
    cid = pick(conn)[0][0] if pick(conn)[0] else pick(conn)[1][0]
    jobs.enqueue_companies(conn, batch, [cid], ["screener"])
    j = jobs.claim(conn, "gh:1:0", ["screener"], "gh", batch)
    d = rules.decide(FetchError("403", 403), attempts=j["attempts"], worker_class="gh")
    assert jobs.fail(conn, j, Exception("403"), d) == "retry"
    st, att, route, hist = status(conn, batch, "screener", cid)
    assert route == "local" and hist[-1]["error_class"] == "blocked"
    assert jobs.claim(conn, "gh:1:1", ["screener"], "gh", batch) is None      # pinned to local
    assert jobs.claim(conn, "local:1:0", ["screener"], "local", batch) is not None


def test_dead_after_max_attempts(conn, batch):
    cid = pick(conn)[0][0] if pick(conn)[0] else pick(conn)[1][0]
    jobs.enqueue_companies(conn, batch, [cid], ["yf"])
    with conn.cursor() as cur:
        cur.execute("update equity.job set max_attempts=1 where batch=%s", (batch,))
    j = jobs.claim(conn, "w", ["yf"], "local", batch)
    d = rules.decide(ValueError("boom"), attempts=j["attempts"], max_attempts=j["max_attempts"])
    assert jobs.fail(conn, j, ValueError("boom"), d) == "dead"


def test_docs_dependency_on_screener(conn, batch):
    with_links, without = pick(conn)
    if not without:
        pytest.skip("every company already has document_link rows")
    cid = without[0]
    jobs.enqueue_companies(conn, batch, [cid], ["screener", "docs_ar"])
    assert jobs.claim(conn, "w", ["docs_ar"], "local", batch) is None          # screener not done yet
    s = jobs.claim(conn, "w", ["screener"], "local", batch)
    assert jobs.claim(conn, "w", ["docs_ar"], "local", batch) is None          # still running
    jobs.complete(conn, s["job_id"])
    d = jobs.claim(conn, "w", ["docs_ar"], "local", batch)
    assert d and d["kind"] == "docs_ar"


def test_docs_claimable_when_links_exist(conn, batch):
    with_links, _ = pick(conn)
    if not with_links:
        pytest.skip("no company with document_link rows")
    jobs.enqueue_companies(conn, batch, [with_links[0]], ["screener", "docs_concall"])
    d = jobs.claim(conn, "w", ["docs_concall"], "local", batch)
    assert d and d["kind"] == "docs_concall"


def test_priority_order(conn, batch):
    ids = [r[0] for r in companies(conn)][:2]
    if len(ids) < 2:
        pytest.skip("need 2 companies")
    jobs.enqueue_companies(conn, batch, [ids[0]], ["yf"], priority=50)
    jobs.enqueue_companies(conn, batch, [ids[1]], ["yf"], priority=5)
    assert jobs.claim(conn, "w", ["yf"], "local", batch)["company_id"] == ids[1]


def test_stale_reclaim(conn, batch):
    cid = companies(conn)[0][0]
    jobs.enqueue_companies(conn, batch, [cid], ["yf"])
    j = jobs.claim(conn, "dead-worker", ["yf"], "local", batch)
    assert jobs.reclaim_stale(conn, 10, batch) == []                            # fresh heartbeat
    with conn.cursor() as cur:
        cur.execute("update equity.job set heartbeat_at = now() - interval '11 minutes' where job_id=%s", (j["job_id"],))
    got = jobs.reclaim_stale(conn, 10, batch)
    assert [g["job_id"] for g in got] == [j["job_id"]] and got[0]["status"] == "retry"
    st, att, route, hist = status(conn, batch, "yf", cid)
    assert st == "retry" and hist[-1]["error_class"] == "stale" and hist[-1]["rule"] == "R10"
    again = jobs.claim(conn, "w2", ["yf"], "local", batch)
    assert again and again["attempts"] == 2
    # heartbeat keeps it alive
    with conn.cursor() as cur:
        cur.execute("update equity.job set heartbeat_at = now() - interval '11 minutes' where job_id=%s", (j["job_id"],))
    jobs.heartbeat(conn, [j["job_id"]])
    assert jobs.reclaim_stale(conn, 10, batch) == []


def test_stale_exhausted_goes_dead(conn, batch):
    cid = companies(conn)[0][0]
    jobs.enqueue_companies(conn, batch, [cid], ["yf"])
    j = jobs.claim(conn, "w", ["yf"], "local", batch)
    with conn.cursor() as cur:
        cur.execute("update equity.job set max_attempts=1, heartbeat_at = now() - interval '1 hour' where job_id=%s",
                    (j["job_id"],))
    assert jobs.reclaim_stale(conn, 10, batch)[0]["status"] == "dead"
