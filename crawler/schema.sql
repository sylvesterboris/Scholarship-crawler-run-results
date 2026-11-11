-- Scholarship Intelligence Crawler — SQLite schema
-- Every value shown in the dashboard can be traced:
--   scholarships -> field_evidence -> snapshots (raw text of the official page) -> sources (URL)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    mode             TEXT NOT NULL,          -- live | archive:<yyyymmdd>
    pages_fetched    INTEGER DEFAULT 0,
    pages_failed     INTEGER DEFAULT 0,
    candidates       INTEGER DEFAULT 0,
    new_records      INTEGER DEFAULT 0,
    changed_records  INTEGER DEFAULT 0,
    unchanged_records INTEGER DEFAULT 0,
    stale_records    INTEGER DEFAULT 0,
    notes            TEXT
);

-- Every URL the crawler has seen (seeds, discovered links, search results).
CREATE TABLE IF NOT EXISTS sources (
    url              TEXT PRIMARY KEY,
    domain           TEXT NOT NULL,
    source_type      TEXT,                   -- Central Government | State Government | University | Corporate CSR | NGO/Trust | International | Aggregator | Unknown
    authority        TEXT,                   -- PRIMARY_OFFICIAL | SECONDARY_OFFICIAL | AGGREGATOR | UNVERIFIED
    discovered_via   TEXT,                   -- seed | link | sitemap | search | aggregator-outlink
    discovered_from  TEXT,                   -- parent URL / search query
    depth            INTEGER DEFAULT 0,
    first_seen       TEXT,
    last_fetched     TEXT,
    last_http_status INTEGER,
    last_error       TEXT,
    content_hash     TEXT,
    page_kind        TEXT                    -- detail | multi | hub | irrelevant | error
);

-- Raw extracted text of each fetched page, kept so evidence can be re-checked later.
CREATE TABLE IF NOT EXISTS snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url              TEXT NOT NULL,
    run_id           INTEGER REFERENCES runs(id),
    fetched_at       TEXT NOT NULL,
    http_status      INTEGER,
    content_type     TEXT,
    content_hash     TEXT,
    title            TEXT,
    text             TEXT,
    links_json       TEXT,                   -- outgoing links (url, anchor) so link evidence can be re-checked
    archived_from    TEXT                    -- Wayback URL when the run replayed an archived copy
);
CREATE INDEX IF NOT EXISTS ix_snap_url ON snapshots(url, content_hash);

CREATE TABLE IF NOT EXISTS scholarships (
    id                    TEXT PRIMARY KEY,  -- stable hash of normalised name + provider
    name                  TEXT NOT NULL,
    name_key              TEXT,              -- year-independent normalised name used to match records across runs
    provider              TEXT,
    provider_domain       TEXT,
    source_type           TEXT,
    official_source_url   TEXT,
    application_url       TEXT,
    benefit               TEXT,
    amount_inr_max        INTEGER,
    education_level       TEXT,
    course                TEXT,
    academic_requirement  TEXT,
    income_criteria       TEXT,
    income_limit_inr      INTEGER,
    age_criteria          TEXT,
    gender_criteria       TEXT,
    category_criteria     TEXT,
    domicile_criteria     TEXT,
    institution_criteria  TEXT,
    disability_criteria   TEXT,
    eligibility_text      TEXT,
    eligibility_rules     TEXT,              -- JSON, machine-readable, built only from evidenced fields
    opening_date          TEXT,
    closing_date          TEXT,
    application_mode      TEXT,
    documents_required    TEXT,
    selection_process     TEXT,
    renewal_requirements  TEXT,
    academic_year         TEXT,
    status_statement      TEXT,              -- e.g. "Applications are closed" quoted from source
    lifecycle_status      TEXT,              -- ACTIVE | EXPIRING_SOON | EXPIRED | REVIEW_REQUIRED | NO_LONGER_VERIFIABLE
    status_reason         TEXT,
    verification_status   TEXT,              -- VERIFIED | REVIEW_REQUIRED
    confidence            REAL,
    score_breakdown       TEXT,              -- JSON list of checks
    first_seen            TEXT,
    last_seen             TEXT,
    last_verified         TEXT,
    last_changed          TEXT,
    consecutive_failures  INTEGER DEFAULT 0,
    current_snapshot_id   INTEGER REFERENCES snapshots(id),
    discovered_via        TEXT
);

-- All pages that mention a scholarship (primary + corroborating + discovery-only).
CREATE TABLE IF NOT EXISTS scholarship_sources (
    scholarship_id   TEXT REFERENCES scholarships(id),
    url              TEXT,
    authority        TEXT,
    role             TEXT,                   -- primary | corroborating | discovery
    snapshot_id      INTEGER,
    closing_date     TEXT,
    amount_inr_max   INTEGER,
    last_seen        TEXT,
    PRIMARY KEY (scholarship_id, url)
);

-- One row per extracted value, with the verbatim text it came from.
CREATE TABLE IF NOT EXISTS field_evidence (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id   TEXT REFERENCES scholarships(id),
    field            TEXT NOT NULL,
    value            TEXT,
    evidence_text    TEXT,                   -- verbatim span from the snapshot
    source_url       TEXT,
    snapshot_id      INTEGER REFERENCES snapshots(id),
    char_start       INTEGER,
    char_end         INTEGER,
    method           TEXT,                   -- rule:<extractor> | llm:<model>+quote-verified | page-meta
    run_id           INTEGER,
    is_current       INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_ev_sch ON field_evidence(scholarship_id, is_current);

-- Nothing is overwritten silently: every difference between runs lands here.
CREATE TABLE IF NOT EXISTS change_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id   TEXT REFERENCES scholarships(id),
    run_id           INTEGER,
    detected_at      TEXT,
    change_type      TEXT,                   -- NEW | FIELD_CHANGED | STATUS_CHANGED | REMOVED_FROM_SOURCE | SOURCE_UNREACHABLE | CONTRADICTED
    field            TEXT,
    old_value        TEXT,
    new_value        TEXT,
    source_url       TEXT,
    old_evidence     TEXT,
    new_evidence     TEXT,
    old_snapshot_id  INTEGER,
    new_snapshot_id  INTEGER
);

CREATE TABLE IF NOT EXISTS verification_checks (
    scholarship_id   TEXT,
    run_id           INTEGER,
    check_name       TEXT,
    points           REAL,
    max_points       REAL,
    passed           INTEGER,
    detail           TEXT
);
CREATE INDEX IF NOT EXISTS ix_vc ON verification_checks(scholarship_id, run_id);
