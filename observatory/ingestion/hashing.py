"""
Content hashing for fetched HTML.

Two hashes per fetch (I-10):
    raw_hash        SHA-256 over the exact bytes received. Forever stable.
    normalized_hash SHA-256 over normalized HTML. Scoped by NORMALIZATION_VERSION.

The normalization version is bumped whenever the normalization rules change.
Old hashes remain valid for their version; new comparisons use the new version.
Old fetches can be re-normalized on demand from their raw bytes (I-8).

v0 normalization rules (NORMALIZATION_VERSION = 1):
    - Decode as UTF-8 with errors='replace' (best-effort; many Indian outlets
      have inconsistent encoding declarations).
    - Strip leading/trailing whitespace.
    - Collapse runs of whitespace (spaces, tabs, newlines) to a single space.
    - Remove well-known per-request noise attributes:
        * data-nonce, data-csrf, data-session, data-cache-buster
        * nonce="..." on script/style tags
        * any attribute whose name starts with "data-ad-"
    - That's it. Deliberately conservative. We can tighten in v2.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

NORMALIZATION_VERSION: Final[int] = 1

# Patterns for v1 normalization. Compiled once.
_WHITESPACE_RUN = re.compile(rb"[ \t\r\n\f]+")
_NOISE_ATTR_NAMES = (
    b"data-nonce",
    b"data-csrf",
    b"data-session",
    b"data-cache-buster",
)
_NONCE_ATTR = re.compile(rb'\snonce="[^"]*"', re.IGNORECASE)
_AD_ATTR = re.compile(rb'\sdata-ad-[a-z0-9_-]+="[^"]*"', re.IGNORECASE)


def hash_raw(content: bytes) -> str:
    """SHA-256 over the exact bytes received. I-10, I-11."""
    return hashlib.sha256(content).hexdigest()


def hash_normalized(content: bytes) -> tuple[str, int]:
    """
    SHA-256 over normalized content + the version of the rules used.
    Returns (hex_digest, version). I-10, I-11.
    """
    normalized = _normalize_v1(content)
    return hashlib.sha256(normalized).hexdigest(), NORMALIZATION_VERSION


def _normalize_v1(content: bytes) -> bytes:
    """v1 normalization rules. See module docstring."""
    out = content

    # Remove known per-request attribute groups.
    for attr in _NOISE_ATTR_NAMES:
        # Match: <whitespace>attrname="anything"
        pattern = re.compile(rb'\s' + re.escape(attr) + rb'="[^"]*"', re.IGNORECASE)
        out = pattern.sub(b'', out)
    out = _NONCE_ATTR.sub(b'', out)
    out = _AD_ATTR.sub(b'', out)

    # Collapse whitespace runs.
    out = _WHITESPACE_RUN.sub(b' ', out)

    # Trim.
    return out.strip()
