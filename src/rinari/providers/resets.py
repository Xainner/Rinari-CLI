"""When a provider limit resets, from whatever the provider said (plan 0.4.0 #24).

Sources, first one that has it wins:

1. the error body: `resets_at` (epoch or ISO) or `resets_in_seconds`, at the
   top or inside `error` (Codex/ChatGPT, OpenCode Go);
2. the headers: `Retry-After`, `x-ratelimit-reset-*` (OpenAI durations like
   `6m0s`) and `anthropic-ratelimit-*-reset` (RFC 3339); when several are
   given, the latest, since all of them must have reset;
3. the Claude CLI's text, «resets 5pm (America/Costa_Rica)».

Everything is returned as an ISO 8601 UTC string, or None.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_CLI_RESET = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([^)]+)\))?",
    re.IGNORECASE,
)
# Nothing sensible resets more than this far ahead; beyond it the value is noise.
_HORIZON = timedelta(days=31)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _from_epoch_or_iso(value: Any, now: datetime) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return _from_epoch_or_iso(float(text), now)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _duration(value: str) -> timedelta | None:
    """`6m0s`, `1s`, `20ms`, `1h2m` (OpenAI's x-ratelimit-reset-*)."""
    parts = _DURATION.findall(value.strip())
    if not parts or "".join(f"{n}{u}" for n, u in parts) != value.strip():
        return None
    total = 0.0
    for number, unit in parts:
        amount = float(number)
        total += {"ms": amount / 1000, "s": amount, "m": amount * 60, "h": amount * 3600}[unit]
    return timedelta(seconds=total)


def from_body(payload: Any, now: datetime) -> datetime | None:
    if not isinstance(payload, Mapping):
        return None
    for holder in (payload, payload.get("error")):
        if not isinstance(holder, Mapping):
            continue
        moment = _from_epoch_or_iso(holder.get("resets_at"), now)
        if moment is not None:
            return moment
        seconds = holder.get("resets_in_seconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0:
            return now + timedelta(seconds=float(seconds))
    return None


def from_headers(headers: Mapping[str, str], now: datetime) -> datetime | None:
    moments: list[datetime] = []
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    for key, value in lowered.items():
        if key.startswith("x-ratelimit-reset"):
            span = _duration(value)
            if span is not None:
                moments.append(now + span)
                continue
            moment = _from_epoch_or_iso(value, now)
            if moment is not None:
                moments.append(moment)
        elif key.startswith("anthropic-ratelimit-") and key.endswith("-reset"):
            moment = _from_epoch_or_iso(value, now)
            if moment is not None:
                moments.append(moment)
    retry = lowered.get("retry-after")
    if retry:
        try:
            moments.append(now + timedelta(seconds=max(0.0, float(retry))))
        except ValueError:
            with contextlib.suppress(TypeError, ValueError):
                moments.append(parsedate_to_datetime(retry).astimezone(UTC))
    return max(moments) if moments else None


def from_cli_text(text: str, now: datetime) -> datetime | None:
    """«resets 5pm (America/Costa_Rica)»: the next such time in that zone."""
    match = _CLI_RESET.search(text or "")
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    # The CLI runs on this machine and speaks its local time; the named zone
    # wins when the zone database has it (Windows often lacks one).
    zone = datetime.now().astimezone().tzinfo or UTC
    if match.group(4):
        with contextlib.suppress(Exception):
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(match.group(4).strip())
    local_now = now.astimezone(zone)
    moment = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment <= local_now:
        moment += timedelta(days=1)
    return moment.astimezone(UTC)


def resets_at(
    *,
    payload: Any = None,
    headers: Mapping[str, str] | None = None,
    text: str | None = None,
    now: datetime | None = None,
) -> str | None:
    current = now or datetime.now(UTC)
    for moment in (
        from_body(payload, current),
        from_headers(headers or {}, current),
        from_cli_text(text or "", current),
    ):
        if moment is not None and current - timedelta(minutes=5) <= moment <= current + _HORIZON:
            return _iso(moment)
    return None


__all__ = ["from_body", "from_cli_text", "from_headers", "resets_at"]
