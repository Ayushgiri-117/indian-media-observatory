"""
Blob storage abstraction.

v0 uses the local filesystem. The interface is intentionally minimal so that
swapping in S3 (or any object store) later is a single-class change.

Layout on disk:
    <root>/
        homepages/<outlet_slug>/<YYYY>/<MM>/<DD>/<UTC-ISO-timestamp>.html
        articles/<outlet_slug>/<YYYY>/<MM>/<DD>/<article_id>/<fetch_id>.html
        sources/<source_id>.<ext>

Constitution touchpoints:
    I-5  Raw HTML is never modified  -> files are written once, never reopened
                                       in write mode. Enforced by convention
                                       here; production should use immutable
                                       filesystem flags or chmod 0444.
    C-9  HTML in object storage, metadata in DB -> the DB stores storage_path,
                                                   never the bytes.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Final


class BlobStore:
    """Write-once blob storage on the local filesystem."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root: Final[Path] = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    # --- Write API ------------------------------------------------------------

    def put_homepage(
        self,
        outlet_slug: str,
        fetched_at: datetime,
        content: bytes,
    ) -> str:
        """Store a homepage snapshot. Returns the storage_path (relative)."""
        self._ensure_utc(fetched_at)
        rel = self._homepage_path(outlet_slug, fetched_at)
        self._write_once(rel, content)
        return str(rel)

    def put_article_fetch(
        self,
        outlet_slug: str,
        article_id: str,
        fetch_id: str,
        fetched_at: datetime,
        content: bytes,
    ) -> str:
        self._ensure_utc(fetched_at)
        rel = self._article_path(outlet_slug, article_id, fetch_id, fetched_at)
        self._write_once(rel, content)
        return str(rel)

    def put_source(
        self,
        source_id: str,
        content: bytes,
        extension: str = "html",
    ) -> str:
        rel = Path("sources") / f"{source_id}.{extension.lstrip('.')}"
        self._write_once(rel, content)
        return str(rel)

    # --- Read API -------------------------------------------------------------

    def get(self, storage_path: str) -> bytes:
        abs_path = self._root / storage_path
        return abs_path.read_bytes()

    # --- Internals ------------------------------------------------------------

    def _homepage_path(self, outlet_slug: str, ts: datetime) -> Path:
        return (
            Path("homepages")
            / outlet_slug
            / f"{ts.year:04d}"
            / f"{ts.month:02d}"
            / f"{ts.day:02d}"
            / f"{ts.strftime('%Y-%m-%dT%H-%M-%S')}Z.html"
        )

    def _article_path(
        self,
        outlet_slug: str,
        article_id: str,
        fetch_id: str,
        ts: datetime,
    ) -> Path:
        return (
            Path("articles")
            / outlet_slug
            / f"{ts.year:04d}"
            / f"{ts.month:02d}"
            / f"{ts.day:02d}"
            / article_id
            / f"{fetch_id}.html"
        )

    def _write_once(self, rel: Path, content: bytes) -> None:
        abs_path = self._root / rel
        if abs_path.exists():
            # I-5: raw HTML is never modified. If we somehow generated the
            # same path twice, that's a bug upstream — fail loudly.
            raise FileExistsError(
                f"Refusing to overwrite blob at {abs_path} (invariant I-5)"
            )
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: write to .tmp then rename, so a crash mid-write
        # never leaves a half-written file claiming to be the raw archive.
        tmp = abs_path.with_suffix(abs_path.suffix + ".tmp")
        tmp.write_bytes(content)
        tmp.rename(abs_path)
        # Best-effort read-only flag. Belt and braces for I-5.
        try:
            os.chmod(abs_path, 0o444)
        except OSError:
            pass

    @staticmethod
    def _ensure_utc(ts: datetime) -> None:
        # I-14: timestamps stored in UTC, always.
        if ts.tzinfo is None or ts.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError(
                f"Timestamp must be UTC-aware (invariant I-14): got {ts!r}"
            )
