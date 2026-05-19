"""
Minimal v0 ingestion: fetch one outlet's homepage, store it, record everything.

What this script does, in order:

    1. Look up the outlet entity in the DB (must already exist; seeded by schema).
    2. Record a fetch_attempts row for the attempt (I-22).
    3. Make the HTTP request.
    4. On success:
        a. Compute raw_hash and normalized_hash (I-10).
        b. Write the bytes to the blob store (I-5: write-once).
        c. Insert a homepage_snapshots row (I-6: append-only).
        d. Update the fetch_attempts row's outcome to 'success' and link the snapshot.
    5. On failure:
        a. Update the fetch_attempts row with the appropriate outcome.
        b. Do NOT write to blob storage.
        c. Do NOT insert a snapshot row.

Note on (4d) and (5a): the constitution says fetch_attempts is append-only.
We resolve this by writing the row only AFTER we know the outcome — one row,
one INSERT, terminal state. The trade-off is that if the process crashes
between the HTTP call and the DB insert, we lose the attempt record. This is
acceptable for v0; in v0.1 we'll move to a two-phase pattern where the attempt
is logged speculatively first, then closed with a status — using two append-
only rows (an 'attempt_started' event and an 'attempt_resolved' event) instead
of a mutable single row.

Usage:
    python -m observatory.ingestion.fetch_homepage ndtv
    python -m observatory.ingestion.fetch_homepage times_now

Exit code 0 on success, 1 on any failure (HTTP error, DB error, etc).
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx
import psycopg
from psycopg.types.json import Jsonb

from observatory.config.settings import (
    CRAWLER_VERSION,
    OUTLETS,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    blob_root,
    database_dsn,
)
from observatory.ingestion.hashing import (
    NORMALIZATION_VERSION,
    hash_normalized,
    hash_raw,
)
from observatory.storage.blob_store import BlobStore


def utcnow() -> datetime:
    """UTC-aware now(). I-14."""
    return datetime.now(timezone.utc)


def fetch_homepage(outlet_slug: str) -> int:
    """Fetch one outlet's homepage. Returns process exit code."""
    if outlet_slug not in OUTLETS:
        print(f"Unknown outlet: {outlet_slug}", file=sys.stderr)
        print(f"Known: {sorted(OUTLETS.keys())}", file=sys.stderr)
        return 1

    outlet = OUTLETS[outlet_slug]
    blob = BlobStore(blob_root())

    # Outcome variables, filled in below.
    attempt_started_at = utcnow()
    outcome: str = "other"
    http_status: int | None = None
    error_detail: str | None = None
    duration_ms: int | None = None
    content: bytes | None = None
    response_headers: dict[str, str] = {}

    # --- 1. Make the HTTP request --------------------------------------------
    t0 = time.monotonic()
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            response = client.get(outlet.homepage_url)
        duration_ms = int((time.monotonic() - t0) * 1000)
        http_status = response.status_code
        response_headers = dict(response.headers)
        if 200 <= response.status_code < 300:
            outcome = "success"
            content = response.content
        else:
            outcome = "http_error"
            error_detail = f"HTTP {response.status_code}"
    except httpx.TimeoutException as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        outcome = "timeout"
        error_detail = repr(e)
    except httpx.ConnectError as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        # Could be DNS or TLS; httpx's exception tree is coarse here.
        outcome = "dns_failure"
        error_detail = repr(e)
    except httpx.RequestError as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        outcome = "other"
        error_detail = repr(e)

    # --- 2. Persist results --------------------------------------------------
    # On success: write blob, insert snapshot row, insert attempt row pointing
    # at the snapshot — all in one DB transaction.
    # On failure: insert only the attempt row.
    try:
        with psycopg.connect(database_dsn()) as conn:
            with conn.cursor() as cur:
                # Look up outlet entity id.
                cur.execute(
                    "SELECT id FROM raw.entities WHERE canonical_name = %s",
                    (outlet.canonical_name,),
                )
                row = cur.fetchone()
                if row is None:
                    print(
                        f"Outlet '{outlet.canonical_name}' not seeded in "
                        f"raw.entities. Run the v0 schema's seed step.",
                        file=sys.stderr,
                    )
                    return 1
                outlet_id: uuid.UUID = row[0]

                snapshot_id: uuid.UUID | None = None

                if outcome == "success":
                    assert content is not None
                    snapshot_id = uuid.uuid4()
                    raw_h = hash_raw(content)
                    norm_h, norm_v = hash_normalized(content)
                    assert norm_v == NORMALIZATION_VERSION

                    # Write blob first; if this fails, we'll record the
                    # attempt as a parse_failure / other below.
                    storage_path = blob.put_homepage(
                        outlet_slug=outlet.slug,
                        fetched_at=attempt_started_at,
                        content=content,
                    )

                    cur.execute(
                        """
                        INSERT INTO raw.homepage_snapshots (
                            id, outlet_id, fetched_at, fetched_url,
                            http_status, raw_hash, normalized_hash,
                            hash_normalization_version, content_length_bytes,
                            content_type, storage_path, response_headers,
                            crawler_version
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            snapshot_id,
                            outlet_id,
                            attempt_started_at,
                            outlet.homepage_url,
                            http_status,
                            raw_h,
                            norm_h,
                            norm_v,
                            len(content),
                            response_headers.get("content-type"),
                            storage_path,
                            Jsonb(response_headers),
                            CRAWLER_VERSION,
                        ),
                    )

                cur.execute(
                    """
                    INSERT INTO raw.fetch_attempts (
                        id, attempted_at, target_url, target_type, outlet_id,
                        outcome, http_status, error_detail, duration_ms,
                        crawler_version, resulting_homepage_snapshot_id
                    ) VALUES (
                        %s, %s, %s, 'homepage', %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        uuid.uuid4(),
                        attempt_started_at,
                        outlet.homepage_url,
                        outlet_id,
                        outcome,
                        http_status,
                        error_detail,
                        duration_ms,
                        CRAWLER_VERSION,
                        snapshot_id,
                    ),
                )

            conn.commit()

    except Exception as e:  # noqa: BLE001 — top-level handler
        print(f"DB error during ingestion: {e!r}", file=sys.stderr)
        return 1

    # --- 3. Report -----------------------------------------------------------
    if outcome == "success":
        print(
            f"OK  {outlet.canonical_name}  "
            f"{http_status}  {len(content):,} bytes  "
            f"raw_hash={raw_h[:12]}...  norm_hash={norm_h[:12]}..."
        )
        return 0
    else:
        print(
            f"ERR {outlet.canonical_name}  "
            f"outcome={outcome}  status={http_status}  detail={error_detail}",
            file=sys.stderr,
        )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch one outlet's homepage.")
    parser.add_argument("outlet_slug", help=f"One of: {sorted(OUTLETS.keys())}")
    args = parser.parse_args()
    return fetch_homepage(args.outlet_slug)


if __name__ == "__main__":
    sys.exit(main())
