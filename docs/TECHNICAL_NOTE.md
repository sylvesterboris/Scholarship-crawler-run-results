# Scholarship Intelligence Crawler — Technical Note

## 1. Architecture

```
            ┌──────────── re-verify every known record first ────────────┐
seeds ─┐    │                                                            ▼
sitemaps ├─► Frontier (priority queue, per-domain budget, depth ≤ 3) ─► Fetcher ─► Snapshot (raw text + links, SQLite)
search ─┘        ▲                                                        │
                 └──── scored links / aggregator out-links / provider chase ◄─┤
                                                                          ▼
                Segmenter (detail | multi-scheme | hub) ─► Extractor (evidence-first) ─► Evidence gate
                                                                          ▼
                Classifier (source type + authority) ─► Merger (primary vs corroborating sources)
                                                                          ▼
                Scorer (deterministic checks) ─► Verification gates ─► Lifecycle status
                                                                          ▼
                Change detector (diff vs stored record) ─► SQLite: scholarships, field_evidence, change_log, ...
                                                                          ▼
                                                    Streamlit dashboard / CSV-JSON export
```

One run is `python run_crawler.py`. `--every 24` runs it on a loop, and a GitHub Actions workflow is included for a daily schedule.

## 2. Technology choices

| Need | Choice | Why |
|---|---|---|
| Fetching | `requests` + `truststore` | Simple and polite. `truststore` uses the OS trust store, which fixes many `.gov.in` sites with incomplete certificate chains without turning off TLS checks. |
| Parsing | BeautifulSoup + lxml, pdfplumber | Many official notifications exist only as PDF guidelines. |
| JS-only pages | Playwright (optional) | Used only when the HTML is an empty JS shell. |
| Storage | SQLite | Free, one file, easy for an evaluator to inspect. |
| UI | Streamlit | Searchable list plus a detail view with evidence and score breakdown. |
| LLM | none by default; optional local Ollama | The core path is deterministic. The LLM can only fill gaps, and only with a verbatim quote that is then re-checked. |

## 3. Discovery methodology

Seeds are entry points (ministry, regulator, state, university, CSR and foundation home or hub pages), never individual
scholarships. Candidates then come from four channels:

1. **Link expansion.** Each link is scored on its anchor text and URL path: positive terms (scholarship, fellowship, post-matric,
   chhatravritti, merit-cum-means, …), helper terms (apply, eligibility, notification) and negative terms (tender, recruitment,
   results, gallery, login, …). Same-site links need a score of 2 or more. Cross-site links are followed only to
   government, university or international domains, or to other sites when the anchor reads like "official website" or "apply".
2. **Sitemaps** of official seed domains, filtered by URL.
3. **Web search** through DuckDuckGo's free HTML endpoint for configured queries. The crawler skips it quietly if blocked, and
   never solves CAPTCHAs.
4. **Aggregator out-links.** Aggregator pages are crawled only to collect links that leave the aggregator for an official domain.

**Provider chase.** When a scholarship is found on a page that is official but not the provider's own (for example a
university notice about a CSR scholarship), the crawler queues links on that page that point to the provider's domain.
That way the primary source can be found and used.

## 4. Extraction methodology

`fetcher` turns HTML into line-structured text (headings `#`/`##`, list items `-`, table cells `|`) and drops navigation,
header and footer. `segment()` labels each page as one of:

* **detail**: one scholarship, named in the h1 or title, with at least 3 signals (eligibility, a non-income ₹ amount, a labelled deadline, an apply or documents section)
* **multi**: several scholarship headings, each followed by at least 2 signals (for example the AICTE schemes page)
* **listing**: an official portal listing many schemes, each with its own labelled dates (e.g. NSP "Schemes on NSP")
* **hub**: links only. These pages are used for discovery, not extraction.

**Listing → linked specification.** On a listing, each entry's own document link ("Specifications", "Guidelines",
"Read more") is aligned to the entry by anchor order. That document (often a PDF on the same official domain) is fetched
and read for eligibility, amount, income limit and provider. Dates are only ever taken from the listing, which is the live
portal, never from an older guideline PDF. Every field keeps its own source URL and snapshot. Gender and category come
only from the entry's own name. A guideline PDF may cover several schemes, so it is not used for those two fields.

Each segment runs field extractors that read section-scoped text first, using headings such as "Eligibility", "Benefits",
"Documents Required", "Selection" and "Renewal", and fall back to the whole segment. Dates are read only when a label
("last date", "closing date", "deadline", "opens on") is on the same line. Deadlines for institutes or nodal officers are
ignored, and an "extended" date wins. Amounts on lines about income are never taken as the scholarship amount. Income
limits are parsed only when limit language appears ("not exceed", "below", "up to"). Education level, category and gender
come from keyword rules applied to the eligibility text, each tied to the line that triggered it. Rules that keep correct
quotes from turning into wrong values:

* "30 % of slots are reserved for girls" is a quota, not a female-only rule.
* "Category certificate (SC/ST/OBC)" is a document requirement, not an eligibility category.
* A date after "dated" is a document date, not a deadline. Bare "till" or "up to" without an application context is ignored.
* An amount next to income language, or inside an income-band table, is not a scholarship amount.
* Scanned PDFs sometimes read "Rs. 12000/-" as "Rs. 120001". Amounts with impossible digit grouping or a stray
  trailing 1 are left unparsed rather than guessed. The verbatim benefit text is kept. The machine-readable
`eligibility_rules` JSON is built **only** from evidenced fields.

## 5. Anti-hallucination approach

1. Each extractor returns `(value, [verbatim spans], method)`. A value without spans cannot exist.
2. **Evidence gate.** Before anything is stored, every span is searched for again in the snapshot text (after whitespace and
   Unicode normalisation). Link evidence must appear in the snapshot's stored link table. A field that fails is dropped,
   and it is stored and shown as `Not specified`.
3. No defaults, and no copying values from other records, aggregators or background knowledge.
4. The optional LLM must return a quote. The quote must exist verbatim on the page, and every number in the value must
   appear inside the quote. Otherwise the answer is discarded.
5. The full chain is stored: `scholarships → field_evidence (value, quote, char offsets, method) → snapshots (text,
   fetched_at, HTTP status) → sources (URL, discovery path)`. The dashboard shows this chain and highlights the quotes
   inside the stored page.

## 6. Verification and confidence score

Source **authority** comes from domain rules plus provider–domain affinity: name/domain containment, acronym
(AICTE ↔ aicte-india.org) or brand token.

* `PRIMARY_OFFICIAL`: the provider's own site, or the government portal that runs the scheme
* `SECONDARY_OFFICIAL`: an official site re-posting someone else's scheme, or a government catalogue such as myScheme
* `UNVERIFIED`: an unknown site whose link to the provider can't be shown
* `AGGREGATOR`: never stored

**Confidence (0–100) is the sum of deterministic checks**. Each check is stored with its points and an explanation:

| Check | Max | How it is measured |
|---|---|---|
| Official source | 25 | PRIMARY 25 · SECONDARY 15 · UNVERIFIED 6 · AGGREGATOR 0 |
| Currently present on source | 15 | 8 if fetched live with HTTP 2xx in this run (4 if archived copy) + 7 × share of name tokens found on the page |
| Official application path | 10 | 5 if the application link is on the official page + 5 if it resolves now; 7 if an offline procedure is stated |
| Critical fields evidenced | 30 | 6 each: provider, benefit, eligibility, education level, deadline-or-status statement |
| Information is current | 10 | 5 if the page names the current academic year (2 if the previous one) + 3 if it has dates within ~13 months + 2 if verified live this run |
| Consistency & conflicts | 10 | 5 if section-scoped and whole-page extraction give the same deadline and amount + 5 if no other official source contradicts the deadline |
| Corroboration bonus | +5 | another independent official domain lists the same scholarship (score is capped at 100) |
| Conflict penalty | −10 | for each unresolved conflict between official sources |

**VERIFIED** requires all of the following. Anything else is **REVIEW_REQUIRED**, with the failed gates listed.

* score ≥ 95
* authority is `PRIMARY_OFFICIAL`
* confirmed on the live page in this run
* no conflicting official source

## 7. Change detection and stale data

Each run starts by re-visiting every stored official URL. Records are matched across runs by a year-independent name key,
so "X Scholarship 2025-26" and "X Scholarship 2026-27" are the same scholarship. Fourteen tracked fields are compared,
including deadline, amount, income limit, application URL, official URL, academic year, lifecycle and verification
status. Every difference is written to `change_log`:

* old value and new value
* run and timestamp
* source URL
* old and new evidence quotes
* old and new snapshot IDs

Evidence rows are versioned (`is_current`), never deleted.

Lifecycle status:

| Status | Rule |
|---|---|
| `EXPIRED` | closing date passed; or the source says it is closed; or only an older academic year is published |
| `EXPIRING_SOON` | closes within 30 days |
| `ACTIVE` | future closing date, or the source states that applications are open |
| `REVIEW_REQUIRED` | no date and no status statement |
| `NO_LONGER_VERIFIABLE` | official page unreachable in 2 consecutive runs (`SOURCE_UNREACHABLE`), or reachable but no longer mentions the scholarship (`REMOVED_FROM_SOURCE`) |

Change types:

| Type | Meaning |
|---|---|
| `FIELD_CHANGED` | the source now states a different value |
| `FIELD_ADDED` / `FIELD_REMOVED` | the source started or stopped stating a value, or a linked document was read for the first time |
| `STATUS_CHANGED` | lifecycle or verification status moved |
| `CONTRADICTED` | a newer official source replaced the previous primary |
| `SOURCE_UNREACHABLE` | the official page could not be fetched |
| `REMOVED_FROM_SOURCE` | the official page no longer mentions the scholarship |

**Real-data demo.** Run 1 replays Wayback Machine copies of the official pages as of 1 Oct 2025. Run 2 crawls the same
pages live. The differences are genuine changes on official sources. The test suite separately proves the mechanics on a
local fixture site, covering a deadline change, a removed page, a new scholarship and an unchanged record.

## 8. Known limitations

* Rule-based extraction misses information that a page publishes only as images or in unusual layouts. Those records fall to
  REVIEW_REQUIRED rather than being guessed, which is deliberate.
* Some state portals are JS-heavy or geo-restricted. Install Playwright and run from an Indian IP for better coverage.
* Provider–domain affinity can't link a provider to a domain with an unrelated name. Such records stay UNVERIFIED, which is
  conservative.
