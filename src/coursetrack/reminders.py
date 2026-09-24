"""Threshold math and the daily-send guards.

Deliberately pure: no network, no clock, no files. `now` is always passed in,
which is what makes `tick --now <timestamp>` able to time-travel through a
whole semester in a test without waiting for a real deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping, Sequence

from .models import Deadline
from .sources.base import is_done


@dataclass(frozen=True, slots=True)
class PendingReminder:
    deadline: Deadline
    # Every threshold newly crossed on this tick, in hours.
    tripped: tuple[int, ...]

    @property
    def label_hours(self) -> int:
        """The most urgent threshold crossed — what the email should say.

        A deadline created inside the 12h window crosses 72 and 12 at once;
        telling the user "3 days left" would be actively wrong.
        """
        return min(self.tripped)


def eligible(
    deadline: Deadline,
    now: datetime,
    *,
    dismissed: Iterable[str] = (),
    submissions: Mapping[str, str] | None = None,
    skip_submitted: bool = True,
    course_enabled=None,
) -> bool:
    """Whether a deadline can generate reminders at all."""
    if deadline.id in set(dismissed):
        return False
    if deadline.due_at <= now:
        return False  # never send a "heads up" for something already due
    if course_enabled is not None and not course_enabled(deadline.course_id):
        return False
    if skip_submitted and is_done((submissions or {}).get(deadline.id, "")):
        return False
    return True


def due_reminders(
    deadlines: Sequence[Deadline],
    now: datetime,
    *,
    thresholds: Sequence[timedelta],
    ledger: Mapping[str, Mapping[str, str]],
    dismissed: Iterable[str] = (),
    submissions: Mapping[str, str] | None = None,
    skip_submitted: bool = True,
    course_enabled=None,
) -> list[PendingReminder]:
    """Reminders that should fire on this tick, one entry per deadline.

    The ledger is what makes this safe to re-run: a delayed, retried or
    manually dispatched run recomputes the same answer and sends nothing.
    """
    dismissed = set(dismissed)
    pending: list[PendingReminder] = []

    for deadline in deadlines:
        if not eligible(
            deadline,
            now,
            dismissed=dismissed,
            submissions=submissions,
            skip_submitted=skip_submitted,
            course_enabled=course_enabled,
        ):
            continue

        already_sent = ledger.get(deadline.id, {})
        tripped = tuple(
            int(t.total_seconds() // 3600)
            for t in thresholds
            if now >= deadline.due_at - t
            and str(int(t.total_seconds() // 3600)) not in already_sent
        )
        if tripped:
            pending.append(PendingReminder(deadline=deadline, tripped=tripped))

    pending.sort(key=lambda p: p.deadline.due_at)
    return pending


def record(ledger: dict[str, dict[str, str]], pending: Iterable[PendingReminder], now: datetime) -> None:
    """Mark every tripped threshold as sent. Call only after a successful send."""
    stamp = now.isoformat()
    for item in pending:
        entry = ledger.setdefault(item.deadline.id, {})
        for hours in item.tripped:
            entry[str(hours)] = stamp


def should_send_brief(now: datetime, last_brief_date: str, brief_hour: int) -> bool:
    """`>=` rather than `==` on purpose.

    GitHub Actions cron is routinely delayed 5-20 minutes and can skip an hour
    entirely under load. Matching `hour == 7` would silently drop the brief on
    those days; the first tick at or after 7am sends it instead, and the date
    guard stops a second one.
    """
    return now.hour >= brief_hour and last_brief_date != now.date().isoformat()


def should_heartbeat(now: datetime, last_heartbeat_date: str, heartbeat_hour: int) -> bool:
    return now.hour >= heartbeat_hour and last_heartbeat_date != now.date().isoformat()


def brief_window(
    deadlines: Sequence[Deadline],
    now: datetime,
    days: int,
    *,
    dismissed: Iterable[str] = (),
    submissions: Mapping[str, str] | None = None,
    skip_submitted: bool = True,
    course_enabled=None,
) -> list[Deadline]:
    """Everything due between now and `days` out, soonest first."""
    horizon = now + timedelta(days=days)
    rows = [
        d
        for d in deadlines
        if eligible(
            d,
            now,
            dismissed=dismissed,
            submissions=submissions,
            skip_submitted=skip_submitted,
            course_enabled=course_enabled,
        )
        and d.due_at <= horizon
    ]
    return sorted(rows, key=lambda d: d.due_at)


def today(now: datetime) -> date:
    return now.date()
