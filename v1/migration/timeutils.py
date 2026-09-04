"""
migration.timeutils
-------------------
Timestamp helpers shared by the checkpoint, the reports, and the read-back
fidelity guard.

Preserving `created_at` and `updated_at` is the point of this tool, so
timestamps are parsed to instants before being compared anywhere — never
compared as strings.
"""

from datetime import datetime, timezone
from typing import Optional


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_gitlab_timestamp(value) -> Optional[datetime]:
    """
    Parse a GitLab ISO-8601 timestamp into a timezone-aware datetime.

    Accepts a `Z` suffix, an explicit numeric offset, and optional fractional
    seconds. A naive value is assumed to be UTC, which is what GitLab serves.

    Args:
        value: Timestamp string, or anything falsy.

    Returns:
        Timezone-aware datetime, or None when value is empty or unparseable.
    """
    if not value:
        return None

    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
