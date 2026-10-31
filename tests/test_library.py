import gzip
import json
from datetime import date

import pytest

from library import index as lib_index
from library import search as lib_search
from library.chunk import assemble, chunk_pages


def write_doc(root, rel, pages):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        for i, t in enumerate(pages, 1):
            f.write(json.dumps({"page": i, "text": t}) + "\n")


def meta(cid, sym, dtype, fy, rel, label=None, url=None):
    return dict(company_id=cid, symbol=sym, name=f"{sym} Ltd", doc_type=dtype, fiscal_year=fy, period_label=label or f"Annual Report {fy}",
                doc_date=date(fy, 6, 1), url=url or f"https://x/{sym}/{fy}.pdf", text_path=rel, page_count=2, char_count=100, pdf_sha256="abc")


FILLER = "The company reported steady growth across its segments during the year. " * 4

PAGES_A = [
    "CHAPTER 1\n\nMANAGEMENT DISCUSSION\n\nOur capital allocation framework prioritises dividends and buybacks. " + FILLER,
    "Capital allocation is reviewed by the board every quarter. " + FILLER + "\n\nRisk factors include currency movements.",
]
PAGES_B = ["Capital expenditure was higher this year as we built new capacity. " + FILLER, "Employees grew to 5000. " + FILLER]
PAGES_C = ["Q2 call. Question about capital allocation and margins. Answer: we will keep returning cash. " + FILLER]


@pytest.fixture()
def lib(tmp_path):
    docs = [
        meta(1, "AAA", "annual_report", 2024, "data/documents/AAA/annual_report/a.jsonl.gz"),
        meta(2, "BBB", "annual_report", 2023, "data/documents/BBB/annual_report/b.jsonl.gz"),
        meta(1, "AAA", "concall_transcript", 2025, "data/documents/AAA/concall_transcript/c.jsonl.gz", label="Jun 2025"),
        meta(3, "CCC", "annual_report", 2022, "data/documents/CCC/annual_report/missing.jsonl.gz"),
    ]
    write_doc(tmp_path, docs[0]["text_path"], PAGES_A)
    write_doc(tmp_path, docs[1]["text_path"], PAGES_B)
    write_doc(tmp_path, docs[2]["text_path"], PAGES_C)
    db = tmp_path / "lib.sqlite"
    stats = lib_index.build(db, docs=docs, root=tmp_path, workers=1, quiet=True)
    return db, docs, tmp_path, stats


def test_chunk_exact_partition():
    pages = PAGES_A + PAGES_B + ["", "x " * 3000]
    text, starts = assemble(pages)
    for target in (200, 600, 1800):
        chunks = chunk_pages(pages, target)
        assert "".join(c["text"] for c in chunks) == text
        assert [c["seq"] for c in chunks] == list(range(len(chunks)))
        assert all(len(c["text"]) <= target for c in chunks)
        assert all(1 <= c["page_from"] <= c["page_to"] <= len(pages) for c in chunks)
        assert [c["page_from"] for c in chunks] == sorted(c["page_from"] for c in chunks)


def test_chunk_headings():
    chunks = chunk_pages(PAGES_A, 300)
    assert any(c["heading"] and "MANAGEMENT DISCUSSION" in c["heading"] for c in chunks)
    assert chunk_pages([]) == [] and chunk_pages(["   "]) == []


def test_index_counts_and_incremental(lib):
    db, docs, root, stats = lib
    assert stats["indexed"] == 3 and stats["missing_file"] == 1 and stats["docs_total"] == 3 and stats["chunks_total"] >= 3
    again = lib_index.build(db, docs=docs, root=root, workers=1, quiet=True)
    assert again["indexed"] == 0 and again["unchanged"] == 3 and again["chunks_added"] == 0
    # the missing file lands later: only that document is indexed
    write_doc(root, docs[3]["text_path"], ["Capital allocation at CCC is conservative. " + FILLER])
    third = lib_index.build(db, docs=docs, root=root, workers=1, quiet=True)
    assert third["indexed"] == 1 and third["unchanged"] == 3 and third["docs_total"] == 4
    # a document that is no longer ok is dropped
    fourth = lib_index.build(db, docs=docs[:2], root=root, workers=1, quiet=True)
    assert fourth["removed"] == 2 and fourth["docs_total"] == 2


def test_search_strict_then_relaxed(lib):
    db = lib[0]
    r = lib_search.search("capital allocation", limit=10, db_path=db)
    hits = r["hits"]
    strict = [h for h in hits if h["strict"]]
    relaxed = [h for h in hits if not h["strict"]]
    assert strict and relaxed
    assert hits[: len(strict)] == strict            # strict matches always come first
    assert all(h["score"] > 1 for h in strict) and all(h["score"] < 1 for h in relaxed)
    assert r["total_strict"] == len(strict)
    assert {h["symbol"] for h in strict} == {"AAA"}   # BBB only has "capital expenditure"
    assert any(h["symbol"] == "BBB" for h in relaxed)


def test_search_filters_and_citation(lib):
    db = lib[0]
    r = lib_search.search("capital allocation", symbols=["aaa"], doc_types=["concall_transcript"], limit=5, db_path=db)
    assert r["hits"] and all(h["symbol"] == "AAA" and h["doc_type"] == "concall_transcript" for h in r["hits"])
    assert r["hits"][0]["citation"] == "AAA · Concall · Jun 2025 · p.1"
    r = lib_search.search("capital allocation", fy_from=2024, fy_to=2024, db_path=db)
    assert {h["fiscal_year"] for h in r["hits"] if h["strict"]} == {2024}
    ar = lib_search.search("capital allocation", symbols=["AAA"], doc_types=["annual_report"], db_path=db)["hits"][0]
    assert ar["citation"].startswith("AAA · Annual Report · FY2024 · p.")
    assert lib_search.search("zzzzqqq", db_path=db)["hits"] == []
    assert lib_search.search("", db_path=db)["hits"] == []
    assert lib_search.search('"unbalanced quote', db_path=db)["hits"] is not None   # never raises


def test_search_phrase_and_snippet_highlight(lib):
    db = lib[0]
    r = lib_search.search('"allocation framework"', limit=5, db_path=db)
    assert len(r["hits"]) == 1 and r["hits"][0]["strict"]
    snip = r["hits"][0]["snippet"]
    assert "<mark>" in snip and "</mark>" in snip and "allocation" in snip.lower()
    f = lib_search.facets("capital allocation", db_path=db)
    assert {c["symbol"] for c in f["companies"]} == {"AAA"}
    assert lib_search.parse_query("What is the capital allocation?") == [("capital", False), ("allocation", False)]
    assert lib_search.query_terms('"free cash flow" dividends') == ["free", "cash", "flow", "dividends"]
