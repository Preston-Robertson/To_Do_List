"""Single-user application clock and timezone policy."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "America/New_York"


def timezone_name() -> str:
    return os.environ.get("LUIGI_WEB_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE


def user_timezone() -> ZoneInfo:
    name = timezone_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"LUIGI_WEB_TIMEZONE is not a valid IANA timezone: {name}") from exc


def local_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(user_timezone())


def local_today() -> date:
    return local_now().date()


def parse_timestamp_local(value: str) -> datetime:
    """Parse a timestamp into configured local time.

    Legacy Luigi Web timestamps were naive values produced by an UTC-configured
    LXC, so naive persisted timestamps are interpreted as UTC. New timestamps
    include their offset and convert unambiguously.
    """
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(user_timezone())


def local_date_from_timestamp(value: str) -> str:
    return parse_timestamp_local(value).date().isoformat()