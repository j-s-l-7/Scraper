"""Smoke test of every Console endpoint against the real DB (TCS and RELIANCE). Skipped when DATABASE_URL is unavailable."""
import pytest

from screener_scraper import config

try:
    config.database_url()
    HAVE_DB = True
except Exception:
    HAVE_DB = False

pytestmark = pytest.mark.skipif(not HAVE_DB, reason="DATABASE_URL not set")

from fastapi.testclient import TestClient  # noqa: E402

from console.api.app import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def get(client, url, status=200):
    r = client.get(url)
    assert r.status_code == status, (url, r.status_code, r.text[:300])
    return r.json()


def test_health(client):
    j = get(client, "/api/health")
    assert j["ok"] is True and "library" in j


def test_securities_list(client):
    j = get(client, "/api/securities")
    assert j["count"] == 100 and len(j["items"]) == 100
    first = j["items"][0]
    for k in ("rank", "symbol", "name", "sector", "market_cap_cr", "price", "pe", "roe_pct", "roce_pct", "ret_1y", "has_screener", "has_yf", "ar_n", "cc_n"):
        assert k in first
    syms = {r["symbol"] for r in j["items"]}
    assert {"TCS", "RELIANCE"} <= syms
    srt = get(client, "/api/securities?sort=market_cap_cr&desc=true")["items"]
    caps = [r["market_cap_cr"] for r in srt if r["market_cap_cr"] is not None]
    assert caps == sorted(caps, reverse=True)
    get(client, "/api/securities?sort=name;drop", 400)


@pytest.mark.parametrize("sym", ["TCS", "RELIANCE"])
def test_company_endpoints(client, sym):
    p = get(client, f"/api/securities/{sym}")
    assert p["profile"]["symbol"] == sym and p["profile"]["name"] and p["snapshot"]["market_cap_cr"] > 0
    assert isinstance(p["growth"], list) and p["yf"] is not None

    f = get(client, f"/api/securities/{sym}/financials?section=profit-loss")
    assert f["rows"] and f["columns"] and f["period"] == "A"
    r0 = f["rows"][0]
    assert len(r0["values"]) == len(f["columns"]) and r0["unit"] in {"cr", "pct", "rs", "days", "count", "x"}
    assert [c["period_end"] for c in f["columns"]] == sorted(c["period_end"] for c in f["columns"])
    assert any(r["children"] for r in f["rows"])
    assert f["ttm"] is None or f["ttm"]["label"] == "TTM"
    for section in ("quarters", "balance-sheet", "cash-flow", "ratios"):
        assert get(client, f"/api/securities/{sym}/financials?section={section}")["rows"], section
    y = get(client, f"/api/securities/{sym}/financials?source=yfinance&section=yf-income")
    assert y["source"] == "yfinance" and y["rows"]
    get(client, f"/api/securities/{sym}/financials?section=pg_sleep", 400)
    get(client, f"/api/securities/{sym}/financials?basis=X", 400)

    pr = get(client, f"/api/securities/{sym}/prices?range=max")
    assert 0 < len(pr["points"]) <= 1500 and len(pr["points"][0]) == 3 and pr["stats"]["last"] > 0
    assert len(get(client, f"/api/securities/{sym}/prices?range=1y")["points"]) < 400
    get(client, f"/api/securities/{sym}/prices?range=forever", 400)

    v = get(client, f"/api/securities/{sym}/valuation")
    assert v["dates"] and "pe" in v["series"] and len(v["series"]["pe"]) == len(v["dates"]) and "pe" in v["summary"]

    s = get(client, f"/api/securities/{sym}/shareholding")
    assert s["periods"] and "promoters" in s["series"] and len(s["series"]["promoters"]) == len(s["periods"])
    assert isinstance(s["holders"], list)

    a = get(client, f"/api/securities/{sym}/actions")
    assert isinstance(a["actions"], list) and "dividends_by_year" in a
    e = get(client, f"/api/securities/{sym}/estimates")
    assert "estimates" in e and isinstance(e["earnings"], list)

    d = get(client, f"/api/securities/{sym}/documents")
    assert d["count"] > 0 and {"key", "doc_type", "status", "period_label"} <= set(d["items"][0])

    q = get(client, f"/api/securities/{sym}/quality")
    assert q["coverage"]["nse_symbol"] == sym and q["completeness"] and isinstance(q["tasks"], list) and isinstance(q["missing_docs"], list)


def test_unknown_symbol(client):
    get(client, "/api/securities/NOPE123", 404)
    get(client, "/api/securities/%27%3B--", 400)


def test_document_reader(client):
    docs = get(client, "/api/securities/TCS/documents")["items"]
    with_file = [d for d in docs if d["has_file"]]
    if not with_file:
        pytest.skip("no local TCS document files yet")
    d = with_file[0]
    j = get(client, f"/api/documents/{d['key']}?page=1&limit=2")
    assert j["available"] and j["meta"]["symbol"] == "TCS" and j["total_pages"] >= 1 and 1 <= len(j["pages"]) <= 2
    assert j["pages"][0]["page"] == 1 and isinstance(j["pages"][0]["text"], str)
    f = get(client, f"/api/documents/{d['key']}/find?q=the")
    assert f["total"] > 0 and f["pages"][0]["page"] >= 1
    get(client, "/api/documents/not-a-key", 404)


def test_library(client):
    st = get(client, "/api/library/stats")
    assert "docs" in st and "chunks" in st
    j = get(client, "/api/library/search?q=capital+allocation&limit=3")
    assert {"hits", "total_strict", "terms", "facets", "stats"} <= set(j)
    for h in j["hits"]:
        assert {"citation", "snippet", "score", "key", "page_from", "symbol"} <= set(h)
    assert get(client, "/api/library/search?q=")["hits"] == []
    get(client, "/api/library/search?q=x&types=bogus", 400)
    get(client, "/api/library/search?q=x&symbols=a%27b", 400)


def test_ops(client):
    j = get(client, "/api/ops")
    assert j["runs"] and j["db"]["bytes"] > 0 and j["db"]["cap_mb"] == 500
    assert j["completeness"]["companies"] == 100 and "library" in j and isinstance(j["jobs"], list)


def test_spa_fallback_and_redirects(client):
    r = client.get("/s/TCS", follow_redirects=False)
    assert r.status_code in (302, 307) and "company" in r.headers["location"]
    assert client.get("/api/nothing-here").status_code == 404
