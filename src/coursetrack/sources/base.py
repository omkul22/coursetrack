"""The seam every deadline source plugs into.

Gradescope is deferred to v2 but will arrive as another `Source` — nothing
outside this package should need to change when it does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..models import Deadline


@dataclass(slots=True)
class FetchResult:
    deadlines: list[Deadline] = field(default_factory=list)
    # deadline_id -> submission workflow_state, for reminder suppression.
    submissions: dict[str, str] = field(default_factory=dict)
    # course_id -> display name, so the dashboard can list courses that
    # currently have no upcoming work.
    courses: dict[str, str] = field(default_factory=dict)


class Source(Protocol):
    name: str

    def fetch(self, now: datetime) -> FetchResult: ...


def is_done(workflow_state: str) -> bool:
    """Whether a submission state means "no more reminders needed"."""
    return workflow_state in {"submitted", "graded", "pending_review"}
