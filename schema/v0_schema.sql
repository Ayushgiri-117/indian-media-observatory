-- =============================================================================
-- Indian Media Observatory — v0 Schema
-- =============================================================================
-- Implements the invariants in data_model_and_invariants.md v0.2 that require
-- database-level enforcement. Conventions and application-layer rules are
-- enforced in code, not here.
--
-- Scope of v0:
--   entities, ownership_edges, sources, articles, article_fetches,
--   fetch_attempts, homepage_snapshots
--
-- Out of scope for v0 (deferred to later versions):
--   authors, categories, unreachability_event evaluator, liveness_probes,
--   crawler_config_versions, entity_aliases workflow, article_content_links,
--   derived-layer tables
-- =============================================================================

-- Two schemas: raw (untouchable archive) and derived (regeneratable).
-- v0 only populates `raw`. `derived` exists from day one so I-9 holds.
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS derived;

-- UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =============================================================================
-- Append-only enforcement (I-4, I-5, I-6, I-22, I-23)
-- =============================================================================
-- Trigger function that blocks UPDATE and DELETE on append-only tables.
-- Attached selectively below.
CREATE OR REPLACE FUNCTION raw.reject_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Append-only table %.%: % not permitted (invariant I-4/I-5/I-6)',
        TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;


-- =============================================================================
-- ENTITIES (I-15, I-16)
-- =============================================================================
-- Entities are NOT append-only at v0 — the entity table holds canonical
-- identity, and we may correct a canonical name. The *aliases* and the *edges*
-- pointing at entities are append-only / time-bounded; that's what protects
-- historical integrity.
CREATE TABLE raw.entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name  TEXT NOT NULL,
    entity_type     TEXT NOT NULL
        CHECK (entity_type IN ('ultimate_owner', 'holding_or_operating_company', 'outlet')),
    -- An entity can have multiple types in reality (C-2). For v0 we keep a
    -- single primary type plus a free-text `also_acts_as` for the rare cases.
    also_acts_as    TEXT[] NOT NULL DEFAULT '{}',
    notes           TEXT,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_entities_type ON raw.entities(entity_type);
CREATE INDEX idx_entities_name ON raw.entities(canonical_name);


-- =============================================================================
-- SOURCES (I-1, I-2)
-- =============================================================================
-- Evidence for every ownership claim. Stores our own copy (storage_path) plus
-- a hash of what we saw, so the evidence trail is reproducible (I-2).
-- Append-only.
CREATE TABLE raw.sources (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type        TEXT NOT NULL
        CHECK (source_type IN ('mca_filing', 'sebi_filing', 'annual_report',
                               'news_article', 'wikipedia', 'manual', 'other')),
    title              TEXT NOT NULL,
    source_url         TEXT,                          -- live URL, NOT canonical
    storage_path       TEXT NOT NULL,                 -- our copy on disk (C-12)
    content_hash       TEXT NOT NULL,                 -- SHA-256 of stored copy
    source_date        DATE,                          -- date document claims
    fetched_at         TIMESTAMP WITH TIME ZONE NOT NULL,
    notes              TEXT,
    created_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TRIGGER sources_append_only
    BEFORE UPDATE OR DELETE ON raw.sources
    FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation();

CREATE INDEX idx_sources_type ON raw.sources(source_type);
CREATE INDEX idx_sources_date ON raw.sources(source_date);


-- =============================================================================
-- OWNERSHIP EDGES (I-1, I-4, I-12, I-13)
-- =============================================================================
-- Time-bounded edges with mandatory source evidence.
-- Append-only: corrections produce new rows with supersedes_id.
CREATE TABLE raw.ownership_edges (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity_id     UUID NOT NULL REFERENCES raw.entities(id),
    to_entity_id       UUID NOT NULL REFERENCES raw.entities(id),
    edge_type          TEXT NOT NULL
        CHECK (edge_type IN ('owns', 'operates', 'publishes')),
    ownership_stake    NUMERIC(6,3)                   -- e.g. 64.710 (%)
        CHECK (ownership_stake IS NULL
               OR (ownership_stake >= 0 AND ownership_stake <= 100)),
    valid_from         DATE NOT NULL,                 -- I-12
    valid_to           DATE,                          -- nullable = current
    source_id          UUID NOT NULL REFERENCES raw.sources(id),  -- I-1
    supersedes_id      UUID REFERENCES raw.ownership_edges(id),
    notes              TEXT,
    created_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CHECK (from_entity_id <> to_entity_id),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TRIGGER ownership_edges_append_only
    BEFORE UPDATE OR DELETE ON raw.ownership_edges
    FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation();

CREATE INDEX idx_edges_from ON raw.ownership_edges(from_entity_id);
CREATE INDEX idx_edges_to ON raw.ownership_edges(to_entity_id);
CREATE INDEX idx_edges_validity ON raw.ownership_edges(valid_from, valid_to);


-- =============================================================================
-- ARTICLES (I-3, I-7, I-15, I-17, I-20, I-21)
-- =============================================================================
-- Per-publication identity (I-17). One outlet's editorial act of publishing.
-- Articles are NOT append-only at the row level because metadata like
-- `deleted_at_source_detected_at` is updated when the deletion is observed
-- (I-7 — soft delete). But the article's *content history* lives entirely
-- in article_fetches, which IS append-only.
CREATE TABLE raw.articles (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    outlet_id                       UUID NOT NULL REFERENCES raw.entities(id),
    canonical_url                   TEXT NOT NULL,
    first_seen_at                   TIMESTAMP WITH TIME ZONE NOT NULL,
    published_at                    TIMESTAMP WITH TIME ZONE,  -- what the article claims
    raw_byline                      TEXT,                       -- free-text for v0 (Q-7)
    raw_category                    TEXT,                       -- free-text for v0
    wire_source_id                  UUID REFERENCES raw.entities(id),    -- I-21
    syndication_source_id           UUID REFERENCES raw.entities(id),    -- I-20
    deleted_at_source_detected_at   TIMESTAMP WITH TIME ZONE,            -- I-7
    created_at                      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE (outlet_id, canonical_url)   -- I-17 scoping
);

CREATE INDEX idx_articles_outlet ON raw.articles(outlet_id);
CREATE INDEX idx_articles_published ON raw.articles(published_at);
CREATE INDEX idx_articles_first_seen ON raw.articles(first_seen_at);


-- =============================================================================
-- ARTICLE FETCHES (I-3, I-5, I-6, I-10, I-11, I-19)
-- =============================================================================
-- Every successful fetch of an article's content. Append-only.
-- Raw bytes live on disk (storage_path); we store hashes + metadata here.
CREATE TABLE raw.article_fetches (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id                  UUID NOT NULL REFERENCES raw.articles(id),
    fetched_at                  TIMESTAMP WITH TIME ZONE NOT NULL,
    fetched_url                 TEXT NOT NULL,        -- URL we actually hit
    http_status                 INTEGER NOT NULL,
    raw_hash                    TEXT NOT NULL,        -- I-10: SHA-256 of bytes
    normalized_hash             TEXT NOT NULL,        -- I-10: SHA-256 of normalized
    hash_normalization_version  INTEGER NOT NULL,     -- I-11
    content_length_bytes        BIGINT NOT NULL,
    content_type                TEXT,
    storage_path                TEXT NOT NULL,        -- C-9: raw HTML on disk
    response_headers            JSONB NOT NULL,
    retry_sequence              INTEGER NOT NULL DEFAULT 0,
    crawler_version             TEXT NOT NULL,        -- I-26 (lightweight v0)
    created_at                  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TRIGGER article_fetches_append_only
    BEFORE UPDATE OR DELETE ON raw.article_fetches
    FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation();

CREATE INDEX idx_fetches_article ON raw.article_fetches(article_id);
CREATE INDEX idx_fetches_time ON raw.article_fetches(fetched_at);
CREATE INDEX idx_fetches_raw_hash ON raw.article_fetches(raw_hash);
CREATE INDEX idx_fetches_norm_hash
    ON raw.article_fetches(normalized_hash, hash_normalization_version);


-- =============================================================================
-- HOMEPAGE SNAPSHOTS (I-5, I-6, I-10, I-11)
-- =============================================================================
-- Per-outlet homepage captures on the 2-hour cadence (C-5).
-- Same hashing discipline as article fetches. Append-only.
CREATE TABLE raw.homepage_snapshots (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    outlet_id                   UUID NOT NULL REFERENCES raw.entities(id),
    fetched_at                  TIMESTAMP WITH TIME ZONE NOT NULL,
    fetched_url                 TEXT NOT NULL,
    http_status                 INTEGER NOT NULL,
    raw_hash                    TEXT NOT NULL,
    normalized_hash             TEXT NOT NULL,
    hash_normalization_version  INTEGER NOT NULL,
    content_length_bytes        BIGINT NOT NULL,
    content_type                TEXT,
    storage_path                TEXT NOT NULL,
    response_headers            JSONB NOT NULL,
    crawler_version             TEXT NOT NULL,
    created_at                  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TRIGGER homepage_snapshots_append_only
    BEFORE UPDATE OR DELETE ON raw.homepage_snapshots
    FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation();

CREATE INDEX idx_homepage_outlet ON raw.homepage_snapshots(outlet_id);
CREATE INDEX idx_homepage_time ON raw.homepage_snapshots(fetched_at);
CREATE INDEX idx_homepage_outlet_time
    ON raw.homepage_snapshots(outlet_id, fetched_at DESC);


-- =============================================================================
-- FETCH ATTEMPTS (I-22, I-23)
-- =============================================================================
-- Every fetch attempt, success or failure. The whole point: gaps in the
-- archive are explainable. Absence in this table = the crawler didn't try,
-- which is itself a discoverable bug. Presence with failure = we tried and
-- couldn't. Presence with success = we got the data.
--
-- Linked to either an article_fetch OR a homepage_snapshot on success
-- (nullable, since failures have no resulting fetch row to link to).
CREATE TABLE raw.fetch_attempts (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attempted_at             TIMESTAMP WITH TIME ZONE NOT NULL,
    target_url               TEXT NOT NULL,
    target_type              TEXT NOT NULL
        CHECK (target_type IN ('article', 'homepage', 'discovery', 'other')),
    outlet_id                UUID REFERENCES raw.entities(id),
    outcome                  TEXT NOT NULL
        CHECK (outcome IN ('success', 'http_error', 'timeout',
                           'dns_failure', 'tls_error', 'parse_failure',
                           'blocked', 'other')),
    http_status              INTEGER,
    error_detail             TEXT,
    retry_sequence           INTEGER NOT NULL DEFAULT 0,
    duration_ms              INTEGER,
    crawler_version          TEXT NOT NULL,
    -- On success, links to the resulting fetch row in one of these:
    resulting_article_fetch_id     UUID REFERENCES raw.article_fetches(id),
    resulting_homepage_snapshot_id UUID REFERENCES raw.homepage_snapshots(id),
    created_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    -- Outcome=success must produce exactly one linked fetch row
    -- (or none, if target_type is 'discovery' — listing a URL is success
    -- without a content fetch).
    CHECK (
        (outcome = 'success' AND target_type = 'discovery')
        OR (outcome = 'success' AND (
            (resulting_article_fetch_id IS NOT NULL
             AND resulting_homepage_snapshot_id IS NULL)
         OR (resulting_article_fetch_id IS NULL
             AND resulting_homepage_snapshot_id IS NOT NULL)))
        OR (outcome <> 'success'
            AND resulting_article_fetch_id IS NULL
            AND resulting_homepage_snapshot_id IS NULL)
    )
);

CREATE TRIGGER fetch_attempts_append_only
    BEFORE UPDATE OR DELETE ON raw.fetch_attempts
    FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation();

CREATE INDEX idx_attempts_time ON raw.fetch_attempts(attempted_at);
CREATE INDEX idx_attempts_outlet ON raw.fetch_attempts(outlet_id);
CREATE INDEX idx_attempts_outcome ON raw.fetch_attempts(outcome);
CREATE INDEX idx_attempts_url ON raw.fetch_attempts(target_url);


-- =============================================================================
-- Seed: NDTV and Times Now as outlets (C-4)
-- =============================================================================
-- For v0, we hardcode these. Q-6 (canonicalization workflow) is deferred.
INSERT INTO raw.entities (canonical_name, entity_type, notes) VALUES
    ('NDTV', 'outlet', 'New Delhi Television Limited. Acquired by Adani Group in 2022.'),
    ('Times Now', 'outlet', 'English news channel of The Times Group (Bennett, Coleman & Co.).')
ON CONFLICT DO NOTHING;
