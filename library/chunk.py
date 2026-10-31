"""Split extracted page text into ordered, non-overlapping passages for search and citation.

The chunks are an exact partition of the assembled document text: they are contiguous
slices of it, so `"".join(chunk["text"] ...)` reproduces the text (and therefore also the
whitespace-normalised pages). Nothing is summarised, dropped or repeated.

Where a chunk ends is chosen from ranked break points, best first: the start of a heading,
a paragraph or sentence end, a list item, a line break, and only as a last resort a word
boundary. A chunk is cut at the latest good break that fits in `target_chars`, so
passages end on sentence boundaries whenever the text has them.

Headings are recognised line by line (numbered clauses, CHAPTER/Annexure lines, FAQ
questions, ALL-CAPS lines). A chunk carries the heading it sits under, inherited from an
earlier chunk when it starts mid-section. Detection favours precision: a missed heading
costs little, a wrong one is shown in citations and weighted in search.
"""

import bisect
import re

DEFAULT_TARGET_CHARS = 1800
MAX_HEADING_CHARS = 250

# Quality of a break point, worst to best.
LINE, ITEM, SENTENCE, PARAGRAPH, HEADING = 1, 2, 3, 4, 5


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------- assembling


def _normalise_page(page: str) -> str:
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in page.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):  # keep at most one blank line (a paragraph break)
            out.append(ln)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def assemble(pages: list[str]) -> tuple[str, list[int]]:
    """Join pages with a line break; also return each page's start offset (an empty page
    starts where the next text begins, so offsets stay non-decreasing)."""
    parts: list[str] = []
    starts: list[int] = []
    pos = 0
    for page in pages:
        text = _normalise_page(page)
        if not text:
            starts.append(pos)
            continue
        if parts:
            parts.append("\n")
            pos += 1
        starts.append(pos)
        parts.append(text)
        pos += len(text)
    return "".join(parts), starts


# --------------------------------------------------------------------------- headings

_PAGE_NO = re.compile(r"^(?:page\s+)?\d+(?:\s*(?:of|/)\s*\d+)?$", re.I)
_LEADER = re.compile(r"(?:\.\s?){4,}|…{2,}|_{4,}")  # table-of-contents dot leaders
_NUMERIC_TOKEN = re.compile(r"^\(?[-+]?[\d,.]*\d[\d,.]*%?\)?$")
_VERBS = re.compile(r"\b(shall|should|must|may|will|is|are|am|was|were|has|have|had|can|cannot|would|could)\b")
_SENTENCE_INSIDE = re.compile(r"[.!?]\s+[A-Z]")
_SMALL_WORDS = frozenset("a an and as at by for from in of on or the to with".split())
_INTERROGATIVE = re.compile(
    r"^(?:what|who|whom|whose|which|when|where|why|how|whether|is|are|am|was|were|do|does|did|can|could|"
    r"should|would|will|shall|may|might|has|have|had|must)\b",
    re.I,
)
# "Q.7 What ...", "Question 7: ...", "7. Whether ..." (SEBI's FAQ drops the space: "10.How ...")
_QLABEL = re.compile(r"^(?:Q(?:uestion)?\.?\s*\d{1,3}\s*[.:)]?\s+|\d{1,3}[.)]\s*(?=[A-Za-z]))(\S.*)$")
_LIST_START = re.compile(r"^(?:[ivxlc]{1,4}(?:[.)]|\s+[A-Z])|[a-z][.)])")  # "ii Liquid Fund", "a) ..."
_STRUCT_KEYWORD = re.compile(r"^(chapter|annexure|annex|appendix|schedule|part|section|article|exhibit)\b", re.I)
_DESIGNATOR = re.compile(r"(?:[A-Z]|[IVXLC]{1,6}|\d{1,3}(?:\.\d{1,2})*[A-Za-z]?(?:\s?\([A-Za-z0-9]{1,3}\))*)(?=$|[\s:.\-–—)])")
_NUMBERED_DOTTED = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){1,3})\s+([A-Z“\"(].*)$")  # "4.2 Advertisement code"
_NUMBERED_PLAIN = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})[.)]\s*([A-Z“\"(].*)$")  # "1. Applicability"
_OUTLINE = re.compile(r"^(?:[IVX]{1,5}(?:[.)]\s*|\s+)|[A-Z][.)]\s*)([A-Z].*)$")  # "II Equity Schemes", "A. Eligibility"
_STANDALONE = frozenset(
    "preamble abbreviations definitions introduction background contents glossary foreword preface disclaimer "
    "appendix annexure summary conclusion".split()
) | {"table of contents", "frequently asked questions"}


def _numeric_count(words: list[str]) -> int:
    return sum(1 for w in words if _NUMERIC_TOKEN.match(w))


def _title_like(title: str, *, max_chars: int = 85, max_words: int = 14, strict: bool = False) -> bool:
    """Short, sentence-free text that reads as a title rather than a wrapped paragraph line."""
    t = title.strip()
    if not t or len(t) > max_chars or not (t[0].isupper() or t[0] in "“\"("):
        return False
    if t[-1] in ".,;-–—" or _SENTENCE_INSIDE.search(t) or _LEADER.search(t) or _VERBS.search(t):
        return False
    words = t.split()
    if len(words) > (6 if strict else max_words) or _numeric_count(words) >= 2:
        return False
    if not any(len(w.strip(",.;:()")) >= 3 for w in words):  # "X", "A B": form placeholders
        return False
    if len(t) > 60 and "," in t:  # a long title with a comma is a clause that wrapped
        return False
    if strict:
        capitalised = sum(1 for w in words if w[0].isupper() or w.lower() in _SMALL_WORDS)
        return capitalised / len(words) >= 0.6
    return True


def _all_caps(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 8 or len(s) > 100 or s[-1] in ",;.":
        return False
    if sum(c.isupper() for c in letters) / len(letters) < 0.92:
        return False
    if _numeric_count(s.split()) >= 2 or sum(c.isdigit() for c in s) > 0.3 * len(s) or re.search(r"\d[,.]\d", s):
        return False  # circular numbers, amounts, table rows
    return True


def _structural(s: str) -> bool:
    m = _STRUCT_KEYWORD.match(s)
    if not m:
        return False
    rest = s[m.end() :].strip()
    if not rest:
        return len(s) <= 40
    d = _DESIGNATOR.match(rest)
    if not d:
        return False
    after = rest[d.end() :].lstrip(" :.-–—)").strip()
    if not after:
        return len(s) <= 60
    return len(s) <= 100 and _title_like(after, max_chars=90, max_words=12)


def _single_line_heading(s: str, next_line: str | None, typical_width: float | None) -> bool:
    if not s or len(s) > 300 or _PAGE_NO.match(s) or _LEADER.search(s):
        return False
    if _structural(s):
        return True
    # A wrapped paragraph's first line ("1. As per extant ...") runs nearly the full line width
    # and the next line carries on in lower case; titles are shorter and followed by new text.
    continues = bool(next_line) and next_line[0].islower() and not _LIST_START.match(next_line)
    wide = typical_width is not None and len(s) >= 0.8 * typical_width
    for rx, max_chars in ((_NUMBERED_DOTTED, 100), (_NUMBERED_PLAIN, 70)):
        m = rx.match(s)
        if m:
            title = m.group(2)
            if title.endswith("?"):
                return len(s) <= 200
            if continues or (wide and rx is _NUMBERED_PLAIN):
                return False
            return _title_like(title, max_chars=max_chars)
    m = _OUTLINE.match(s)
    if m and not continues and not wide:
        return _title_like(m.group(1), max_chars=60, strict=True)
    if re.sub(r"[\s:.\-–]+$", "", s).lower() in _STANDALONE:
        return True
    return _all_caps(s)


def _question_heading(lines: list[str], i: int) -> tuple[int, str] | None:
    """FAQ question ("Q.7 ..." or "7. Whether ...?"), which often wraps over 2-3 lines."""
    first = lines[i].strip()
    m = _QLABEL.match(first)
    if not m:
        return None
    labelled = first[0] in "Qq"
    if not labelled and not _INTERROGATIVE.match(m.group(1)):
        return None
    parts, j = [first], i
    while not parts[-1].endswith("?") and j - i < 3 and j + 1 < len(lines):
        nxt = lines[j + 1].strip()
        if not nxt or parts[-1][-1:] in ".!;":
            break
        parts.append(nxt)
        j += 1
    text = " ".join(parts)
    if text.endswith("?"):
        return j + 1, text
    return (i + 1, first) if labelled and len(first) <= 160 else None


def _tidy(heading: str) -> str:
    h = normalize_whitespace(heading).rstrip(" :")
    return h if len(h) <= MAX_HEADING_CHARS else h[: MAX_HEADING_CHARS - 1].rstrip() + "…"


def median_line_width(lines: list[str]) -> float | None:
    """Median line length of prose-like text; None for table-like text (short lines), where a
    line's length says nothing about whether it is a wrapped paragraph."""
    widths = sorted(len(ln) for ln in lines if ln)
    median = widths[len(widths) // 2] if widths else 0
    return float(median) if median >= 50 else None


def find_headings(lines: list[str], typical_width: float | None = None) -> list[tuple[int, int, str]]:
    """(first line, end line exclusive, heading text) for every heading in `lines`.
    `typical_width` is the document's median line length, used to tell a short title from the
    full-width first line of a wrapped paragraph."""
    found: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        hit: tuple[int, str] | None = None
        if s and not _LEADER.search(s):
            hit = _question_heading(lines, i)
            if hit is None and _single_line_heading(s, lines[i + 1].strip() if i + 1 < len(lines) else None, typical_width):
                hit = (i + 1, s)
        if hit:
            end, text = hit
            found.append((i, end, _tidy(text)))
            i = end
        else:
            i += 1
    return found


# --------------------------------------------------------------------------- break points

_SENT_END = re.compile(r"([.!?])([\"”’')\]]*)(\s+)(?=[A-Z0-9“\"‘(\[•])")
_ITEM_START = re.compile(r"^(?:[•\-–*▪◦]\s|\(?[a-z]{1,2}\)\s|\(?[ivxlc]{1,5}\)\s|\d{1,2}[.)]\s)")
_ABBREVIATIONS = frozenset(
    "rs re no nos sr dr mr mrs ms shri smt st vs viz ie eg cf ltd pvt co inc corp approx dt cl reg regs sec para paras "
    "art fig vol pp ref amt govt dept hon prof tel jan feb mar apr jun jul aug sep sept oct nov dec".split()
)


def _is_abbreviation(text: str, dot: int) -> bool:
    """Is the '.' at `dot` the end of an abbreviation, initial or list marker, not of a sentence?"""
    k = dot
    while k > 0 and not text[k - 1].isspace() and text[k - 1] not in "([\"“‘'":
        k -= 1
    token = text[k:dot]
    low = token.lower()
    if low in _ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
        return True
    if "." in token and all(len(p) <= 2 for p in token.split(".")):  # i.e. / w.e.f
        return True
    at_line_start = k == 0 or text[k - 1] == "\n"
    return at_line_start and (token.isdigit() or re.fullmatch(r"[ivxlc]+", low) is not None)


def _boilerplate(lines: list[str], starts: list[int], page_starts: list[int]) -> set[int]:
    """Indices of running headers/footers and page numbers: lines (digits ignored) that sit among
    the first or last two lines of at least 30% of the pages. They stay in the text; they are
    only kept out of page ranges and section cuts, so a chunk never claims the next page just
    because that page's header trails it."""
    pages: dict[int, list[int]] = {}
    for idx, line in enumerate(lines):
        if line:
            pages.setdefault(bisect.bisect_right(page_starts, starts[idx]), []).append(idx)
    if len(pages) < 4:
        return set()
    edge = {p: [i for i in idxs[:2] + idxs[-2:] if len(lines[i]) <= 120] for p, idxs in pages.items()}
    seen: dict[str, set[int]] = {}
    for p, idxs in edge.items():
        for i in idxs:
            seen.setdefault(re.sub(r"\d+", "#", lines[i].lower()), set()).add(p)
    need = max(3, int(0.3 * len(pages)))
    return {i for idxs in edge.values() for i in idxs if len(seen[re.sub(r"\d+", "#", lines[i].lower())]) >= need}


def _break_points(
    text: str, lines: list[str], starts: list[int], headings: list[tuple[int, int, str]], cut_lines: list[int]
) -> tuple[list[int], list[int]]:
    """Ranked break points as parallel sorted lists. `cut_lines[i]` is the line where a cut before
    heading i should happen (its own line, or the page header lines directly above it)."""
    ranks: dict[int, int] = {}

    def put(pos: int, rank: int) -> None:
        if rank > ranks.get(pos, 0):
            ranks[pos] = rank

    for idx in range(1, len(lines)):
        if not lines[idx]:
            continue
        put(starts[idx], PARAGRAPH if not lines[idx - 1] else LINE)
        if _ITEM_START.match(lines[idx]):
            put(starts[idx], ITEM)
    for m in _SENT_END.finditer(text):
        if m.group(1) == "." and _is_abbreviation(text, m.start()):
            continue
        put(m.end(), PARAGRAPH if "\n\n" in m.group(3) else SENTENCE)
    span_lo = [starts[first] for first, _, _ in headings]
    span_hi = [starts[end] if end < len(starts) else len(text) for _, end, _ in headings]
    for cut_line in cut_lines:
        if cut_line > 0:
            put(starts[cut_line], HEADING)
    positions = []
    for p in sorted(ranks):
        i = bisect.bisect_right(span_lo, p) - 1
        if i >= 0 and span_lo[i] < p < span_hi[i]:  # never cut inside a (multi-line) heading
            continue
        positions.append(p)
    return positions, [ranks[p] for p in positions]


def _next_cut(text: str, start: int, positions: list[int], ranks: list[int], target: int) -> int:
    n = len(text)
    if n - start <= target:
        return n
    limit = start + target
    lo, hi = bisect.bisect_right(positions, start), bisect.bisect_right(positions, limit)
    forced, good, floor = max(1, target // 4), max(1, target // 2), max(1, target // 5)
    for k in range(lo, hi):  # a section boundary, once the chunk is big enough to stand alone
        if ranks[k] == HEADING and positions[k] - start >= forced:
            return positions[k]

    def latest(min_rank: int, min_offset: int) -> int | None:
        for k in range(hi - 1, lo - 1, -1):
            if positions[k] - start < min_offset:
                return None
            if ranks[k] >= min_rank:
                return positions[k]
        return None

    for min_rank, min_offset in ((SENTENCE, good), (ITEM, good), (SENTENCE, floor), (LINE, floor), (LINE, 1)):
        cut = latest(min_rank, min_offset)
        if cut is not None:
            return cut
    space = max(text.rfind(" ", start + 1, limit), text.rfind("\n", start + 1, limit))
    return space + 1 if space > start else limit  # a single token longer than the target is cut hard


# --------------------------------------------------------------------------- public


def chunk_pages(pages: list[str], target_chars: int = DEFAULT_TARGET_CHARS) -> list[dict]:
    """Chunks `{"seq", "page_from", "page_to", "heading", "text"}` in document order.

    `seq` counts from 0 and pages from 1. No chunk is longer than `target_chars`. A chunk can
    span pages (page_from < page_to) because sentences do; page numbers ignore running page
    headers/footers, which stay in the text. `heading` is the section that holds most of the
    chunk: normally the one it starts in (inherited when the chunk starts mid-section).
    """
    if target_chars < 1:
        raise ValueError("target_chars must be positive")
    text, page_starts = assemble(pages)
    if not text:
        return []
    lines = text.split("\n")
    starts: list[int] = []
    pos = 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    headings = find_headings(lines, median_line_width(lines))
    boiler = _boilerplate(lines, starts, page_starts)

    def page_of(offset: int) -> int:
        return bisect.bisect_right(page_starts, offset)  # 1-based: pages up to and including this one

    cut_lines = []
    for first, _, _ in headings:  # a cut before a heading goes above the page header lines that precede it
        k = first
        while k > 0 and (k - 1) in boiler and page_of(starts[k - 1]) == page_of(starts[first]):
            k -= 1
        cut_lines.append(k)
    positions, ranks = _break_points(text, lines, starts, headings, cut_lines)
    head_offsets = [starts[first] for first, _, _ in headings]
    head_cuts = [starts[k] for k in cut_lines]
    head_ends = [starts[end] if end < len(starts) else len(text) for _, end, _ in headings]
    head_texts = [h for _, _, h in headings]

    def content_span(a: int, b: int) -> tuple[int, int]:
        """First and last offsets of real content in text[a:b] (page headers/footers skipped)."""
        i0, i1 = bisect.bisect_right(starts, a) - 1, bisect.bisect_right(starts, b - 1) - 1
        rows = [i for i in range(i0, i1 + 1) if lines[i]]
        real = [i for i in rows if i not in boiler] or rows or [i0]
        lo = max(a, starts[real[0]])
        hi = min(b, starts[real[-1]] + len(lines[real[-1]])) - 1
        return lo, max(lo, hi)

    def heading_for(lo: int, hi: int) -> str | None:
        """The section holding most of text[lo..hi]: the one in effect at `lo` (inherited when the
        chunk starts mid-section), unless a section that starts inside the chunk covers more."""
        in_effect = bisect.bisect_right(head_offsets, lo) - 1
        best, best_cover = None, 0
        for i in range(max(in_effect, 0), bisect.bisect_right(head_offsets, hi)):
            begin = lo if i == in_effect else head_offsets[i]
            end = min(head_offsets[i + 1], hi + 1) if i + 1 < len(head_offsets) else hi + 1
            if end - begin > best_cover:
                best, best_cover = head_texts[i], end - begin
        return best

    chunks: list[dict] = []
    start = 0
    while start < len(text):
        cut = _next_cut(text, start, positions, ranks, target_chars)
        lo, hi = content_span(start, cut)
        k = bisect.bisect_right(head_offsets, hi) - 1
        if cut < len(text) and k >= 0 and head_cuts[k] > start and hi < head_ends[k]:
            cut = head_cuts[k]  # the chunk would end on a heading: keep it with the text it introduces
            lo, hi = content_span(start, cut)
        chunks.append(
            {
                "seq": len(chunks),
                "page_from": page_of(lo),
                "page_to": page_of(hi),
                "heading": heading_for(lo, hi),
                "text": text[start:cut],
            }
        )
        start = cut
    return chunks
