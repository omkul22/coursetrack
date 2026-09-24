"""The one record shared by every source, and the redaction rule for it.

Stable IDs are what make the whole system idempotent: calendar reconciliation
and the reminder ledger both key off `Deadline.id`, so the same assignment seen
on two different runs must produce byte-identical IDs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime

CANVAS = "canvas"
MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Deadline:
    """A single thing that is due at a single moment.

    `due_at` is always timezone-aware. Naive datetimes are rejected at
    construction rather than allowed to propagate, because a naive datetime
    silently compared against an aware `now` is the kind of bug that only
    shows up as a reminder that fired four hours late.
    """

    id: str
    source: str
    course: str
    course_id: str
    title: str
    due_at: datetime
    url: str | None = None
    private: bool = False

    def __post_init__(self) -> None:
        if self.due_at.tzinfo is None or self.due_at.utcoffset() is None:
            raise ValueError(f"due_at must be timezone-aware, got {self.due_at!r}")

    @property
    def is_manual(self) -> bool:
        return self.source == MANUAL


def canvas_id(course_id: int | str, assignment_id: int | str) -> str:
    return f"{CANVAS}:{course_id}:{assignment_id}"


def manual_id() -> str:
    return f"{MANUAL}:{uuid.uuid4()}"
