# Observatory v0

The minimal end-to-end skeleton for the Indian Media Observatory.

This is **Step 2** of the plan:

1. Design v0 schema. — `schema/v0_schema.sql`
2. **Implement minimal ingestion.** — `ingestion/fetch_homepage.py`
3. Revise based on what reality teaches us.

## What works in v0

- Fetch one outlet's homepage (NDTV or Times Now).
- Compute raw and normalized SHA-256 hashes (I-10, I-11).
- Store the raw HTML to disk under a write-once layout (I-5).
- Insert a `homepage_snapshots` row plus a `fetch_attempts` row in one transaction.
- On any failure (HTTP error, timeout, DNS), still record the `fetch_attempts` row so the gap is explainable (I-22).

## What's deliberately missing

- Article-level ingestion. Comes after we trust homepage ingestion.
- Retry logic. We log one attempt per run; the scheduler that drives retries is v0.1.
- Liveness probes (I-25). Comes with the scheduler.
- Unreachability event evaluator (I-24). Comes once we have failure data to test against.
- Entity canonicalization workflow (Q-6). Entities are seeded manually.
- The derived layer. The schema reserves `derived` but populates nothing.

## Setup

You need PostgreSQL 15+ running locally with a database to point at.

```bash
# 1. Install dependencies
pip install -e .

# 2. Create a database (one-time)
createdb observatory
# or whatever you like; set OBSERVATORY_DATABASE_URL to match

# 3. Apply the schema
psql "$OBSERVATORY_DATABASE_URL" -f schema/v0_schema.sql

# 4. Choose where raw HTML lives
export OBSERVATORY_BLOB_ROOT=~/observatory-data
```

## Run it

```bash
python -m observatory.ingestion.fetch_homepage ndtv
python -m observatory.ingestion.fetch_homepage times_now
```

Expected output on success:

```
OK  NDTV  200  187,432 bytes  raw_hash=8f3a2c1b...  norm_hash=4d1e9f8a...
```

## Verify the invariants held

Run these queries and confirm:

```sql
-- I-3, I-22: every attempt has a row, success or fail
SELECT outcome, COUNT(*) FROM raw.fetch_attempts GROUP BY outcome;

-- I-10: both hashes always populated
SELECT COUNT(*) FROM raw.homepage_snapshots
WHERE raw_hash IS NULL OR normalized_hash IS NULL;
-- expected: 0

-- I-14: timestamps are UTC
SELECT fetched_at, fetched_at AT TIME ZONE 'UTC' AS as_utc
FROM raw.homepage_snapshots LIMIT 5;

-- I-4: try to update a snapshot; should error
UPDATE raw.homepage_snapshots SET http_status = 999 WHERE TRUE;
-- expected: ERROR: Append-only table raw.homepage_snapshots: UPDATE not permitted
```

And on disk:

```bash
# I-5: stored files should be read-only
ls -l "$OBSERVATORY_BLOB_ROOT"/homepages/ndtv/*/*/*/
```

## Project layout

```
observatory/
  schema/
    v0_schema.sql            # DDL, triggers, seed data
  config/
    settings.py              # outlets, crawler version, DB DSN
  storage/
    blob_store.py            # write-once filesystem blob store
  ingestion/
    hashing.py               # raw + normalized hashing (versioned)
    fetch_homepage.py        # the minimal end-to-end script
  pyproject.toml
  README.md
```

## What to look for in the first real run

Things that are likely to break or surprise you:

- **NDTV serves region-specific content.** The HTML you get from a server in Bangalore is different from what you get from one in Delhi or from a Cloudflare edge. The raw hash will not match between runs from different machines, and that's correct — both are real fetches, both go in the archive.
- **Times Now redirects.** `timesnownews.com` does a few hops. `follow_redirects=True` handles this; the URL that ends up in `fetched_url` is still the configured homepage URL, not the final URL after redirects. We may want to record the final URL separately in v0.1.
- **Content-Length headers lie.** We record `content_length_bytes` from the actual bytes received, not the header.
- **Cloudflare / bot protection.** If we get a 403 or a JS challenge page, that's recorded as either `http_error` or (worse) `success` with a tiny payload that's actually a challenge page. v0 won't catch the latter; the size will look suspicious and we'll know to add detection in v0.1.
- **Encoding declarations.** Outlets sometimes declare `charset=utf-8` and serve windows-1252. Our v1 normalization decodes with `errors='replace'`, so this is logged but doesn't crash.

Note any of these in `data_model_and_invariants.md` under Open Questions before changing code.
