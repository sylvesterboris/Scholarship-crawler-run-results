# Scholarship Intelligence Crawler

A working crawler that keeps a repository of **authentic scholarship opportunities for Indian students** up to date:

```
Discover → Crawl → Extract → Verify → Score → Store → Update (re-verify + change/stale detection)
```

* Every value in the database is linked to the **verbatim sentence on the official page** it came from
  (`scholarships → field_evidence → snapshots → sources`). If the official page doesn't state something,
  the field says **`Not specified`**. Nothing is guessed.
* Confidence is a **deterministic, additive score** built from checks you can see (official domain, live presence,
  application link resolves, evidence for critical fields, freshness, consistency). No LLM produces a number.
  **VERIFIED needs ≥ 95 %**, the provider's own official page, live presence in this run, and no conflicting official source.
* Aggregators (Buddy4Study, news sites, blogs) are used **only to discover links to official sites**. They never feed data into a record.
* Every run re-checks known records first, then discovers new ones. Differences are written to `change_log`
  with old value, new value, source and both evidence quotes. Old data is not overwritten silently.

100 % free stack: Python, requests, BeautifulSoup/lxml, pdfplumber, SQLite, Streamlit. Optional extras: Playwright for JS-only pages, and a local Ollama model as a quote-verified gap filler.

---

## What the crawler found

These are the results of the three runs in `data/scholarships.db`, crawled 12 Nov 2026 from a Mac in Delhi NCR. Run 1 replayed Wayback copies of the official pages as of 1 Oct 2025, run 2 was a live crawl, and run 3 was a live re-verification.

| Requirement | Result |
|---|---|
| Real scholarships | **90** |
| Primary source is the provider's own official site or administering portal | **78** |
| Confidence ≥ 95 % (VERIFIED) | **16** |
| Source types | **6**: Central Govt 36, State Govt 21, International 16, NGO/Trust 13, Corporate CSR 2, University 2 |
| Lifecycle | EXPIRING_SOON 24, ACTIVE 2, EXPIRED 28, NO_LONGER_VERIFIABLE 3, REVIEW_REQUIRED 33 |
| Discovery | 77 of 90 records came from pages the crawler found itself (73 by link following, 4 from sitemaps). Only 13 came from the seed entry pages themselves. |

**Real changes detected** between the 2025 archive and the live pages:

| Scholarship | Field | Old | New |
|---|---|---|---|
| Santoor Women's Scholarship | closing date | 2025-10-15 | 2026-10-15 |
| The Rhodes Scholarships for India | closing date | 2025-07-23 | 2026-07-23 |
| The Rhodes Scholarships for India | opening date | 2025-06-02 | 2026-06-01 |
| Savitribai Jyotirao Phule Fellowship (UGC) | fellowship rate | ₹31,000 / ₹35,000 pm | ₹37,000 / ₹42,000 pm |
| Kotak Kanya Scholarship | opening date | 2025-06-02 | 2026-07-14 |
| Kotak Kanya Scholarship | academic year | 2025-26 | 2026-27 |
| Reliance Foundation Scholarships | source | older official page | newer 2026-27 announcement (`CONTRADICTED`) |

**Stale detection:**

* 3 records went `NO_LONGER_VERIFIABLE` after their official pages failed in two consecutive runs: AICTE PMSSS, AICTE PG (GATE/GPAT), and PMRF (HTTP 403).
* 28 records are `EXPIRED`, from published closing dates or "Applications closed" statements (for example Kotak Kanya, Santoor, Inlaks).

Every value can be traced: open a record in the dashboard to see the verbatim quote and the stored page it came from.

**Known imperfections**, kept visible rather than hidden:

* The education-level keyword rules over-include in a few records (e.g. "Undergraduate, Postgraduate" on the Reliance PG scholarship).
* Some providers are only as specific as the source document allows. "Government of India" appears where a guideline PDF names no ministry.
* AICTE's main site reset most connections from this network, so AICTE schemes come via NSP and `aicte.gov.in`.
* DuckDuckGo blocked automated search, so discovery ran on links, sitemaps and provider-chase only.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run_crawler.py                 # one live crawl (≈ 10–20 min with the default 450-page budget)
streamlit run app/dashboard.py        # open http://localhost:8501
```

Run it from a machine in India if you can. Some `.gov.in` portals refuse foreign or datacentre IP ranges.

### Auto-crawler mode

```bash
python run_crawler.py --every 24      # crawl once a day, forever
# or cron:   0 7 * * *  cd /path/to/repo && .venv/bin/python run_crawler.py
# or GitHub Actions: .github/workflows/crawl.yml (daily, commits the updated DB)
```

### Change-detection demo against real official pages

```bash
rm -f data/scholarships.db
python run_crawler.py --archive 20250915 --max-pages 200   # Run 1: Wayback Machine copies of the official pages as of 15 Sep 2025
python run_crawler.py                                      # Run 2: the same official pages live today
```

Run 2 re-visits every record from Run 1 and logs what changed, for example a new deadline, a new academic year, an
amount, a page that has gone (`SOURCE_UNREACHABLE` → `NO_LONGER_VERIFIABLE`), or a scheme that has disappeared from its page
(`REMOVED_FROM_SOURCE`). Archive runs can never produce `VERIFIED`, because "currently present on the official source"
can only be confirmed live.

### Full demo (what produced `data/scholarships.db`)

```bash
bash run_demo.sh      # venv + deps (+ optional Playwright), archive run, live run, re-verification run, export
```

### Tests

```bash
pytest -q
# 1) fixture site, 3 runs: deadline change, removed page, new scholarship, "Not specified" for unstated fields
# 2) portal listing + linked specification documents: per-field provenance, dates only from the live listing
```

---

## Repository layout

| Path | What it is |
|---|---|
| `run_crawler.py` | CLI: one run, `--every N` hours, `--archive YYYYMMDD`, `--max-pages` |
| `crawler/discovery.py` | Prioritised frontier fed by seeds, link scoring, sitemaps, DuckDuckGo HTML search, aggregator out-links |
| `crawler/fetcher.py` | Polite fetcher (robots.txt, per-domain delay), HTML/PDF → line-structured text, optional Playwright, Wayback replay |
| `crawler/classifier.py` | Source type (Central/State Govt, University, CSR, NGO/Trust, International, Aggregator) and authority (PRIMARY / SECONDARY official / AGGREGATOR / UNVERIFIED) |
| `crawler/extractor.py` | Page segmentation (detail / multi-scheme / hub) and evidence-first field extraction |
| `crawler/llm.py` | Optional local-LLM gap filler: quote must be verbatim on page, numbers must come from the quote |
| `crawler/verifier.py` | Evidence gate, confidence score, verification gates, lifecycle status |
| `crawler/pipeline.py` | Orchestration, merging of sources, change detection, stale detection |
| `crawler/schema.sql` | Database schema |
| `app/dashboard.py` | Streamlit dashboard |
| `config/settings.yaml` | Budget, seeds (entry points), search queries, thresholds |
| `config/domains.yaml` | Domain rules (gov/university suffixes, state markers, aggregator list) |
| `data/scholarships.db` | The SQLite repository produced by the crawler |
| `sample_data/` | CSV/JSON export with evidence and change history (`python scripts/export_records.py`) |
| `docs/TECHNICAL_NOTE.md` | 3-page technical note |

## Inspecting the database

```bash
sqlite3 data/scholarships.db
.mode box
SELECT name, provider, closing_date, lifecycle_status, verification_status, confidence FROM scholarships ORDER BY confidence DESC;
-- trace one value back to the official page
SELECT field, value, evidence_text, source_url, snapshot_id FROM field_evidence WHERE scholarship_id='<id>' AND is_current=1;
SELECT * FROM change_log WHERE change_type!='NEW';
```
