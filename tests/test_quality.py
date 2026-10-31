import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg2
import pytest

from screener_scraper import config, quality as Q


def d(y, m, dd=1):
    return date(y, m, dd)


def test_reconcile_tolerances():
    pairs = {(1, d(2025, 3, 31)): (100.0, 101.0),        # 1% ok
             (2, d(2025, 3, 31)): (100.0, 96.0),         # 4% -> warn
             (3, d(2025, 3, 31)): (100.0, 80.0),         # 20% -> error
             (4, d(2025, 3, 31)): (0.0, 0.0)}
    out = {i.company_id: i for i in Q.check_reconcile("recon_sales", pairs)}
    assert set(out) == {2, 3} and out[2].severity == "warn" and out[3].severity == "error"
    assert out[3].detail["periods"][0]["diff_pct"] == 20.0
    assert Q.check_reconcile("recon_eps", {(1, d(2025, 3, 31)): (0.1, 0.4)}, min_abs=0.5) == []


def test_identities():
    assert Q.check_balance_identity({(1, "C", d(2025, 3, 31)): (1000.0, 1000.4)}) == []
    bad = Q.check_balance_identity({(1, "C", d(2025, 3, 31)): (1000.0, 900.0)})
    assert len(bad) == 1 and bad[0].check == "identity_balance"
    assert Q.check_cashflow_identity({(1, "C", d(2025, 3, 31)): (10.0, 100.0, -60.0, -29.0)}) == []
    assert len(Q.check_cashflow_identity({(1, "C", d(2025, 3, 31)): (50.0, 100.0, -60.0, -29.0)})) == 1
    assert Q.check_opm({(1, "C", "A", d(2025, 3, 31)): (25.0, 250.0, 1000.0)}) == []
    assert len(Q.check_opm({(1, "C", "A", d(2025, 3, 31)): (40.0, 250.0, 1000.0)})) == 1


def test_ohlc():
    ok = ("A", d(2025, 1, 1), 10, 12, 9, 11)
    bad = ("B", d(2025, 1, 1), 10, 9, 8, 9.5)           # open above high
    bad2 = ("B", d(2025, 1, 2), 10, 12, 11, 11.5)       # open below low
    out = Q.check_ohlc([ok, bad, bad2])
    assert [i.key for i in out] == ["B"] and out[0].detail["n_bad"] == 2


def test_returns_ignore_splits():
    cands = [("X", d(2025, 6, 10), -0.5), ("Y", d(2025, 6, 10), 0.6), ("Z", d(2025, 6, 10), 0.3)]
    out = Q.check_returns(cands, {"X": [d(2025, 6, 9)]})
    assert [i.key for i in out] == ["Y"]


def test_calendar_gaps_and_stale():
    days = [d(2025, 1, 1) + timedelta(days=i) for i in range(30)]
    counts = {x: 10 for x in days}
    counts[days[7]] = 1                                    # holiday for most
    cal = Q.trading_calendar(counts, 10)
    assert days[7] not in cal and len(cal) == 29
    have = [x for x in cal if x != days[12]]
    out = Q.check_missing_days({"S": have, "P": cal[20:]}, cal)
    assert [i.key for i in out] == ["S"] and out[0].detail["dates"] == [days[12].isoformat()]
    st = Q.check_stale({"A": d(2025, 1, 29), "B": d(2025, 1, 20)}, d(2025, 1, 30))
    assert [i.key for i in st] == ["B"]


def test_close_mismatch():
    out = Q.check_close_mismatch({("A", d(2025, 1, 1)): 100.0, ("B", d(2025, 1, 1)): 100.2},
                                 {("A", d(2025, 1, 1)): 101.0, ("B", d(2025, 1, 1)): 100.0})
    assert [i.key for i in out] == ["A"]


def test_find_gaps_and_completeness():
    q = [d(2025, 3, 31), d(2025, 6, 30), d(2025, 12, 31)]
    assert Q.find_gaps(q, 3) == [d(2025, 9, 30)]
    assert Q.find_gaps([d(2023, 3, 31), d(2025, 3, 31)], 12) == [d(2024, 3, 31)]
    assert Q.find_gaps([d(2025, 3, 31)], 3) == []
    qd = {1: [d(2025, 3, 31), d(2025, 6, 30), d(2025, 9, 30)], 2: [d(2025, 3, 31), d(2025, 6, 30), d(2025, 9, 30)],
          3: [d(2025, 3, 31), d(2025, 6, 30)]}
    out = Q.check_completeness(qd, {1: [d(2023, 3, 31), d(2025, 3, 31)]}, d(2025, 11, 1))
    got = {(i.check, i.company_id) for i in out}
    assert ("latest_quarter_missing", 3) in got and ("year_gap", 1) in got and ("latest_quarter_missing", 1) not in got


def test_outliers():
    s = {(1, "C:1"): [(d(2025, 3, 31), 100.0), (d(2025, 6, 30), 1500.0), (d(2025, 9, 30), 1400.0)],
         (2, "C:1"): [(d(2025, 3, 31), 100.0), (d(2025, 6, 30), 400.0)],
         (3, "C:1"): [(d(2025, 3, 31), 1.0), (d(2025, 6, 30), 50.0)]}          # base below 5 cr: ignored
    assert [i.company_id for i in Q.check_outliers(s)] == [1]


def test_docs_gap():
    r = {"company_id": 1, "ar_expected": 12, "ar_found": 6, "ar_missing": [2015, 2016], "cc_expected": 40,
         "cc_found": 10, "cc_missing": [d(2016, 6, 30)], "ppt": 0}
    out = Q.check_docs([r, {"company_id": 2, "ar_expected": 12, "ar_found": 12, "ar_missing": [], "cc_expected": 40,
                            "cc_found": 40, "cc_missing": []}])
    assert len(out) == 1 and out[0].company_id == 1 and out[0].detail["ar_missing"] == [2015, 2016]


def test_scorecard_math():
    full = Q.score_company(q_count=13, a_count=12, price_days_behind=0, q_behind=False, open_errors=0, open_warns=0,
                           docs_ratio=1.0)
    assert full["overall"] == 100.0
    bad = Q.score_company(q_count=0, a_count=0, price_days_behind=30, q_behind=True, open_errors=4, open_warns=0,
                          docs_ratio=None)
    assert bad["overall"] == 0.0 and bad["consistency"] == 0.0
    mid = Q.score_company(q_count=13, a_count=12, price_days_behind=5, q_behind=False, open_errors=1, open_warns=1,
                          docs_ratio=0.5)
    assert mid["coverage"] == 100 and mid["freshness"] == 75 and mid["consistency"] == 67.0 and mid["documents"] == 50


def test_plan_action_bounds_and_backoff():
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    assert Q.plan_action("docs_gap", [], now) == ("docs_retry", ())
    h = [{"action": "docs_retry", "at": (now - timedelta(days=1)).isoformat()}]
    assert Q.plan_action("docs_gap", h, now) is None                       # backing off (needs 3 days)
    h = [{"action": "docs_retry", "at": (now - timedelta(days=4)).isoformat()}]
    assert Q.plan_action("docs_gap", h, now) == ("docs_retry", ())
    h4 = [{"action": "docs_retry", "at": (now - timedelta(days=30 + i)).isoformat()} for i in range(4)]
    assert Q.plan_action("docs_gap", h4, now) is None                      # attempts exhausted
    assert Q.plan_action("latest_quarter_missing", [], now) == ("rescrape", ("screener",))
    assert Q.plan_action("price_ohlc", [], now) is None                    # not remediable
    h3 = [{"action": "rescrape", "at": (now - timedelta(days=60)).isoformat()}] * 3
    assert Q.plan_action("quarter_gap", h3, now) is None


def test_extra_windows_plan():
    assert Q.plan_extra_windows(1001, 1000) == []
    assert Q.plan_extra_windows(1001, 1700) == [(1001, 1300), (1301, 1600), (1601, 1700)]


def test_universe_latest():
    assert Q.universe_latest({1: d(2025, 9, 30), 2: d(2025, 9, 30), 3: d(2025, 6, 30), 4: d(2025, 12, 31)}) == d(2025, 9, 30)


# ───────── database-backed: ledger upsert/resolve and the PIT trigger (all rolled back / cleaned up) ─────────
@pytest.fixture()
def conn():
    try:
        c = psycopg2.connect(config.database_url(), connect_timeout=15)
    except Exception as e:                      # pragma: no cover
        pytest.skip(f"no database: {e}")
    c.autocommit = True
    yield c
    c.close()


def test_persist_upsert_keeps_history_and_resolves(conn):
    code = "test_" + uuid.uuid4().hex[:6]
    try:
        r = Q.persist(conn, [Q.Issue(code, "warn", 0, "k1", {"x": 1}), Q.Issue(code, "error", 0, "k2", {"x": 2})], [code])
        assert r["new"] == 2 and r["resolved"] == 0
        with conn.cursor() as cur:
            cur.execute("update equity.dq_issue set detail = jsonb_set(detail,'{history}','[{\"action\":\"rescrape\"}]') "
                        "where check_code=%s and key='k1'", (code,))
        r = Q.persist(conn, [Q.Issue(code, "warn", 0, "k1", {"x": 3})], [code])          # k2 no longer reproduces
        assert r["new"] == 0 and r["resolved"] == 1
        with conn.cursor() as cur:
            cur.execute("select key, status, detail from equity.dq_issue where check_code=%s order by key", (code,))
            rows = cur.fetchall()
        assert rows[0][1] == "open" and rows[0][2]["x"] == 3 and rows[0][2]["history"] == [{"action": "rescrape"}]
        assert rows[1][1] == "resolved" and rows[1][2]["history"][-1]["action"] == "resolved"
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from equity.dq_issue where check_code=%s", (code,))


def test_pit_trigger_and_asof(conn):
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("select company_id,basis,metric_id,period_type,period_end,value,updated_at from equity.financial_fact limit 1")
            cid, basis, mid, pt, pe, v, upd = cur.fetchone()
            cur.execute("select count(*) from equity.financial_fact_history where company_id=%s and metric_id=%s and period_end=%s",
                        (cid, mid, pe))
            before = cur.fetchone()[0]
            cur.execute("update equity.financial_fact set value = value where company_id=%s and basis=%s and metric_id=%s "
                        "and period_type=%s and period_end=%s", (cid, basis, mid, pt, pe))      # unchanged: no history
            cur.execute("update equity.financial_fact set value = value + 7 where company_id=%s and basis=%s and metric_id=%s "
                        "and period_type=%s and period_end=%s", (cid, basis, mid, pt, pe))
            cur.execute("select value, recorded_at from equity.financial_fact_history where company_id=%s and basis=%s "
                        "and metric_id=%s and period_type=%s and period_end=%s order by superseded_at desc",
                        (cid, basis, mid, pt, pe))
            h = cur.fetchall()
            assert len(h) == before + 1 and h[0][0] == v and h[0][1] == upd
            cur.execute("select value from equity.financials_asof(%s) where company_id=%s and basis=%s and metric_id=%s "
                        "and period_type=%s and period_end=%s", (upd + timedelta(microseconds=1), cid, basis, mid, pt, pe))
            assert [r[0] for r in cur.fetchall()] == [v]
            cur.execute("select count(*) from equity.v_financials_asof(now())")
            assert cur.fetchone()[0] > 0
    finally:
        conn.rollback()
        conn.autocommit = True
