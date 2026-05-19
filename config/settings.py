"""
v0 configuration.

Hardcoded for now (C-4: NDTV and Times Now only). Becomes a real config file
once we have a second outlet that doesn't fit the pattern.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final


# Bumped whenever crawler behavior changes meaningfully (user-agent, headers,
# retry policy). v0 is a lightweight stand-in for the full crawler_config_versions
# table that comes in v0.1. (I-26)
CRAWLER_VERSION: Final[str] = "v0.1.0"

# A polite, identifiable user agent. We're a research crawler; we say so.
USER_AGENT: Final[str] = (
    "MediaObservatoryBot/0.1 (+research; contact: TBD)"
)

REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0


@dataclass(frozen=True)
class OutletConfig:
    slug: str             # filesystem-safe identifier
    canonical_name: str   # must match raw.entities.canonical_name
    homepage_url: str


OUTLETS: Final[dict[str, OutletConfig]] = {
    "ndtv": OutletConfig(
        slug="ndtv",
        canonical_name="NDTV",
        homepage_url="https://www.ndtv.com/",
    ),
    "times_now": OutletConfig(
        slug="times_now",
        canonical_name="Times Now",
        homepage_url="https://www.timesnownews.com/",
    ),
}


def database_dsn() -> str:
    """
    Connection string for PostgreSQL.

    Reads from $OBSERVATORY_DATABASE_URL, falling back to a local default.
    Example:
        postgresql://observatory:observatory@localhost:5432/observatory
    """
    return os.environ.get(
        "OBSERVATORY_DATABASE_URL",
        "postgresql://observatory:observatory@localhost:5432/observatory",
    )


def blob_root() -> str:
    """Filesystem root for the raw blob store."""
    return os.environ.get(
        "OBSERVATORY_BLOB_ROOT",
        os.path.expanduser("~/observatory-data"),
    )
