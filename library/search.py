"""Full-text search over the local library (SQLite FTS5), following rebirth-assistant's result semantics.

* Strict pass: every term must match (FTS5 implicit AND; "quoted phrases" stay phrases). Score = 1 + squashed bm25,
  so a score above 1 always means all terms matched.
* Relaxed pass: when the strict pass returns fewer than `limit` passages it is topped up from an any-term (OR) query,
  scored bm25 squashed x (share of terms present)^2, always below 1, so strict matches come first.
* Each hit carries a highlighted snippet (`<mark>..</mark>`), a citation ("TCS · Annual Report · FY2025 · p.12") and a score.
Filters (symbols, doc types, FY range) are applied inside the FTS query through the indexed token columns.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .index import DEFAULT_DB, dtype_token, fy_token, sym_token

MAX_LIMIT = 50
MAX_TERMS = 8
MARK_OPEN, MARK_CLOSE = "<mark>", "</mark>"
DOC_TYPE_LABEL = {"annual_report": "Annual Report", "concall_transcript": "Concall", "concall_ppt": "Concall PPT"}

STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i if in into is it its of on or that the their there these this "
    "to was what when where which who whom why will with about any all my your you should would could may might must "
    "not no than then them they we".split())
_SUFFIXES = ("ements", "ement", "ations", "ation", "ings", "ing", "ies", "ied", "ers", "er", "ed", "es", "s", "e", "y")
_TOKEN = re.compile(r'"([^"]+)"|([A-Za-z0-9][A-Za-z0-9&\-\']*)')


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def parse_query(query: str) -> list[tuple[str, bool]]:
    """[(text, is_phrase)] in order. Single words lose stopwords unless nothing else is left."""
    items: list[tuple[str, bool]] = []
    for m in _TOKEN.finditer(query or ""):
        if m.group(1):
            words = re.findall(r"[a-z0-9]+", m.group(1).lower())
            if len(words) > 1:
                items.append((" ".join(words), True))
            elif words:
                items.append((words[0], False))
        else:
            for w in re.findall(r"[a-z0-9]+", m.group(2).lower()):
                items.append((w, False))
    keep = [it for it in items if it[1] or (it[0] not in STOPWORDS and len(it[0]) >= 2)]
    keep = keep or [it for it in items if it[1] or it[0]]
    seen, out = set(), []
    for it in keep:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out[:MAX_TERMS]


def query_terms(query: str) -> list[str]:
    """Plain words (phrases split) for highlighting in the reader."""
    return list(dict.fromkeys(w for t, _ in parse_query(query) for w in t.split()))


def _quote(t: str) -> str:
    return '"' + t.replace('"', '""') + '"'


def _filters(symbols, doc_types, fy_from, fy_to) -> str:
    parts = []
    if symbols:
        parts.append("symtok : (" + " OR ".join(_quote(sym_token(s)) for s in symbols) + ")")
    if doc_types:
        parts.append("dtok : (" + " OR ".join(_quote(dtype_token(t)) for t in doc_types) + ")")
    if fy_from or fy_to:
        lo, hi = int(fy_from or 2000), int(fy_to or 2100)
        hi = min(hi, 2100)
        if hi - lo > 80:
            lo = hi - 80
        parts.append("fytok : (" + " OR ".join(_quote(fy_token(y)) for y in range(lo, hi + 1)) + ")")
    return " AND ".join(parts)


def build_match(items: list[tuple[str, bool]], *, relaxed: bool, symbols=None, doc_types=None, fy_from=None, fy_to=None) -> str | None:
    if not items:
        return None
    joiner = " OR " if relaxed else " AND "
    content = "{heading text} : (" + joiner.join(_quote(t) for t, _ in items) + ")"
    f = _filters(symbols, doc_types, fy_from, fy_to)
    return f"{content} AND {f}" if f else content


def connect_ro(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def citation(symbol: str, doc_type: str, fiscal_year, period_label, page_from, page_to) -> str:
    kind = DOC_TYPE_LABEL.get(doc_type, doc_type)
    period = period_label if doc_type != "annual_report" and period_label else (f"FY{fiscal_year}" if fiscal_year else (period_label or ""))
    pages = f"p.{page_from}" if page_from == page_to or not page_to else f"p.{page_from}-{page_to}"
    return " · ".join(x for x in (symbol, kind, period, pages) if x)


def _squash(rank: float) -> float:
    x = max(0.0, -float(rank))  # bm25() is negative: more relevant is more negative
    return x / (x + 8.0)


_SQL = """
select c.id as chunk_id, c.doc_id, d.key, d.symbol, d.name, d.doc_type, d.fiscal_year, d.period_label, d.doc_date, d.url,
       c.page_from, c.page_to, c.heading, {text} bm25(chunk_fts, 3.0, 1.0, 0.0, 0.0, 0.0) as rank,
       snippet(chunk_fts, 1, '<mark>', '</mark>', ' … ', 48) as snippet
from chunk_fts
join chunk c on c.id = chunk_fts.rowid
join doc d on d.id = c.doc_id
where chunk_fts match ?{exclude}
order by rank, d.doc_date desc, c.ord
limit ? offset ?
"""


def _hit(r: sqlite3.Row, score: float, strict: bool) -> dict:
    return {
        "chunk_id": r["chunk_id"], "doc_id": r["doc_id"], "key": r["key"], "symbol": r["symbol"], "name": r["name"],
        "doc_type": r["doc_type"], "fiscal_year": r["fiscal_year"], "period_label": r["period_label"],
        "doc_date": r["doc_date"], "url": r["url"], "page_from": r["page_from"], "page_to": r["page_to"],
        "heading": r["heading"], "snippet": " ".join((r["snippet"] or "").split()), "score": round(score, 4),
        "strict": strict,
        "citation": citation(r["symbol"], r["doc_type"], r["fiscal_year"], r["period_label"], r["page_from"], r["page_to"]),
    }


def search(q: str, symbols: list[str] | None = None, doc_types: list[str] | None = None, fy_from: int | None = None,
           fy_to: int | None = None, limit: int = 10, offset: int = 0, *, db_path: Path | str = DEFAULT_DB) -> dict:
    """{"hits": [...], "total_strict": n, "terms": [...], "limit", "offset"}; hits are strict matches first."""
    limit = max(0, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    items = parse_query(q)
    out = {"hits": [], "total_strict": 0, "terms": query_terms(q), "limit": limit, "offset": offset}
    if not items or not limit or not Path(db_path).exists():
        return out
    symbols = [s.upper() for s in symbols or []] or None
    con = connect_ro(db_path)
    try:
        flt = dict(symbols=symbols, doc_types=doc_types or None, fy_from=fy_from, fy_to=fy_to)
        strict_q = build_match(items, relaxed=False, **flt)
        try:
            out["total_strict"] = con.execute("select count(*) from chunk_fts where chunk_fts match ?", (strict_q,)).fetchone()[0]
            rows = con.execute(_SQL.format(text="", exclude=""), (strict_q, limit, offset)).fetchall()
        except sqlite3.OperationalError:
            return out  # un-parseable query: no results rather than a 500
        hits = [_hit(r, 1.0 + max(_squash(r["rank"]), 0.001), True) for r in rows]
        need = limit - len(hits)
        # relaxed top-up, only on the first page and only when strict ran dry
        if need > 0 and len(items) > 1 and offset + len(hits) >= out["total_strict"]:
            skip = max(0, offset - out["total_strict"])
            ids = [h["chunk_id"] for h in hits]
            strict_all = {r[0] for r in con.execute(
                "select rowid from chunk_fts where chunk_fts match ? limit 500", (strict_q,))}
            relaxed_q = build_match(items, relaxed=True, **flt)
            pats = [re.compile(r"\b" + re.escape(_stem(t.split()[0])), re.I) for t, _ in items]
            pool = con.execute(_SQL.format(text="c.text as text,", exclude=""), (relaxed_q, (need + skip + len(strict_all)) * 2 + 10, 0)).fetchall()
            scored = []
            for r in pool:
                if r["chunk_id"] in strict_all or r["chunk_id"] in ids:
                    continue
                body = f"{r['heading'] or ''} {r['text']}"
                share = sum(1 for p in pats if p.search(body)) / len(pats)
                scored.append((_squash(r["rank"]) * share ** 2 * 0.999, r))
            scored.sort(key=lambda t: -t[0])
            hits += [_hit(r, s, False) for s, r in scored[skip : skip + need]]
        out["hits"] = hits
        return out
    finally:
        con.close()


def facets(q: str, *, db_path: Path | str = DEFAULT_DB, cap: int = 5000) -> dict:
    """Counts of matching passages (strict match, no filters, top `cap` by rank) by company, doc type and FY."""
    res = {"companies": [], "doc_types": [], "fiscal_years": []}
    items = parse_query(q)
    if not items or not Path(db_path).exists():
        return res
    con = connect_ro(db_path)
    try:
        m = build_match(items, relaxed=False)
        base = ("from (select rowid from chunk_fts where chunk_fts match ? order by rank limit ?) h "
                "join chunk c on c.id = h.rowid join doc d on d.id = c.doc_id")
        try:
            res["companies"] = [dict(symbol=r[0], name=r[1], n=r[2]) for r in con.execute(
                f"select d.symbol, d.name, count(*) n {base} group by d.symbol order by n desc limit 100", (m, cap))]
            res["doc_types"] = [dict(doc_type=r[0], n=r[1]) for r in con.execute(
                f"select d.doc_type, count(*) n {base} group by d.doc_type order by n desc", (m, cap))]
            res["fiscal_years"] = [dict(fiscal_year=r[0], n=r[1]) for r in con.execute(
                f"select d.fiscal_year, count(*) n {base} where d.fiscal_year is not null group by d.fiscal_year order by d.fiscal_year", (m, cap))]
        except sqlite3.OperationalError:
            pass
        return res
    finally:
        con.close()
