"""When a scheduled task runs next.

A schedule is a small JSON object in the machine's local time (the tasks run
on this machine, so its clock and daylight-saving rules are the right ones):

    {"kind": "once", "at": "2026-09-25T09:00"}
    {"kind": "interval", "minutes": 30}
    {"kind": "daily", "time": "08:30"}
    {"kind": "weekly", "days": [0, 2, 4], "time": "08:30"}   # 0 = Monday

Times are epoch seconds everywhere else; only these rules know about local
dates. Pure functions: the scheduler passes its clock in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 7 * 24 * 60
# A run that was due while the app was closed still runs when it comes back,
# but not a stale one: after this it is recorded as skipped.
CATCH_UP_WINDOW_S = 12 * 3600

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
KINDS = ("once", "interval", "daily", "weekly")


class ScheduleError(ValueError):
    """An invalid schedule; the message is safe to show."""


@dataclass(frozen=True)
class Schedule:
    kind: str
    at: datetime | None = None  # once: naive local datetime
    minutes: int = 0  # interval
    hour: int = 0
    minute: int = 0
    days: tuple[int, ...] = ()  # weekly, 0 = Monday

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "once":
            assert self.at is not None
            return {"kind": "once", "at": self.at.strftime("%Y-%m-%dT%H:%M")}
        if self.kind == "interval":
            return {"kind": "interval", "minutes": self.minutes}
        time = f"{self.hour:02d}:{self.minute:02d}"
        if self.kind == "daily":
            return {"kind": "daily", "time": time}
        return {"kind": "weekly", "days": list(self.days), "time": time}


def parse_schedule(raw: Any) -> Schedule:
    if not isinstance(raw, dict):
        raise ScheduleError("The schedule must be an object.")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ScheduleError(f"Schedule kind must be one of {', '.join(KINDS)}.")
    if kind == "once":
        at = raw.get("at")
        if not isinstance(at, str):
            raise ScheduleError("A one-time schedule needs 'at' (YYYY-MM-DDTHH:MM).")
        try:
            moment = datetime.fromisoformat(at)
        except ValueError:
            raise ScheduleError("'at' must be a local date and time (YYYY-MM-DDTHH:MM).") from None
        if moment.tzinfo is not None:
            raise ScheduleError("'at' is local time: leave the timezone out.")
        return Schedule("once", at=moment.replace(second=0, microsecond=0))
    if kind == "interval":
        minutes = raw.get("minutes")
        if (
            not isinstance(minutes, int)
            or isinstance(minutes, bool)
            or not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES
        ):
            raise ScheduleError(
                f"The interval must be {MIN_INTERVAL_MINUTES}..{MAX_INTERVAL_MINUTES} minutes."
            )
        return Schedule("interval", minutes=minutes)
    time = raw.get("time")
    match = _TIME.match(time) if isinstance(time, str) else None
    if match is None:
        raise ScheduleError("'time' must be HH:MM (24 h).")
    hour, minute = int(match.group(1)), int(match.group(2))
    if kind == "daily":
        return Schedule("daily", hour=hour, minute=minute)
    days = raw.get("days")
    if (
        not isinstance(days, list)
        or not days
        or any(
            not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6 for day in days
        )
    ):
        raise ScheduleError("A weekly schedule needs 'days': 0 (Monday) to 6 (Sunday).")
    return Schedule("weekly", hour=hour, minute=minute, days=tuple(sorted(set(days))))


def _local(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch)


def _epoch(moment: datetime) -> float:
    # A naive datetime is local time; `timestamp()` applies the machine's
    # offset for that date, daylight saving included.
    return moment.timestamp()


def next_run(schedule: Schedule, after: float) -> float | None:
    """The first run strictly after `after`, or None when there is none left."""
    if schedule.kind == "once":
        assert schedule.at is not None
        due = _epoch(schedule.at)
        return due if due > after else None
    if schedule.kind == "interval":
        return after + schedule.minutes * 60
    base = _local(after)
    for offset in range(0, 8):
        day = (base + timedelta(days=offset)).replace(
            hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0
        )
        if schedule.kind == "weekly" and day.weekday() not in schedule.days:
            continue
        due = _epoch(day)
        if due > after:
            return due
    return None  # unreachable for valid daily/weekly schedules


def describe(schedule: Schedule) -> str:
    """A short English description (clients localize their own)."""
    if schedule.kind == "once":
        assert schedule.at is not None
        return f"once at {schedule.at.strftime('%Y-%m-%d %H:%M')}"
    if schedule.kind == "interval":
        return f"every {schedule.minutes} min"
    time = f"{schedule.hour:02d}:{schedule.minute:02d}"
    if schedule.kind == "daily":
        return f"daily at {time}"
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return f"{', '.join(names[day] for day in schedule.days)} at {time}"
