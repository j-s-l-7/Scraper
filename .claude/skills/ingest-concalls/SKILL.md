---
name: ingest-concalls
description: Fetch full-verbatim earnings-call (concall) transcripts for an NSE company (screener index → BSE/IR PDF → text) into equity.concalls. Use when asked to add, backfill, or fix concall transcripts, or to find a company's calls.
---

# Concall transcript ingestion

`ingestion/ingest_concalls.py` reads the concall index on a company's screener.in page, downloads each transcript
PDF (BSE / company IR site / Wayback), extracts the full text with pdfplumber, and upserts it into `equity.concalls`.

```bash
python3 ingestion/ingest_concalls.py --symbol TCS
python3 ingestion/ingest_concalls.py --symbol M&M --slug M-M      # screener slug differs from the NSE symbol
```

## Data contract
* PK `(symbol, fiscal_year, quarter)`. FY2026 runs Apr 2025 to Mar 2026, and quarters are Q1–Q4 within the FY.
* `UNIQUE(url)`: when a parser fix relabels a quarter, `upsert()` deletes the stale row that holds the url first.
* `transcript` holds the full text, never a summary. Put summaries in a separate table.
* Raw PDFs are archived to `~/Desktop/claude/backups/concalls_pdfs/<SYMBOL>/`.
* Add URLs missing from screener's index to `data/concall_extra_urls.json` so re-runs reproduce them.

## Related
`equity.document_link` (filled by the screener scraper) already lists the transcript, PPT and recording links for the top-100
universe. Use it to find what still needs ingesting:
```sql
select c.nse_symbol, d.period_label, d.url
from equity.document_link d join equity.company c using (company_id)
where d.doc_type = 'concall_transcript'
  and not exists (select 1 from equity.concalls k where k.url = d.url);
```
