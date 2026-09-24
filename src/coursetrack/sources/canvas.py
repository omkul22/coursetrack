"""Canvas LMS source.

Two endpoints do the whole job:
  GET /api/v1/courses?enrollment_state=active
  GET /api/v1/courses/{id}/assignments?include[]=submission

`include[]=submission` returns the calling user's own submission inline, which
gives submission state for free instead of one extra request per assignment.

The token is sent as a Bearer header and never as a query parameter — query
strings end up in server logs, proxy logs, and exception messages.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Iterator

import httpx

from ..models import CANVAS, Deadline, canvas_id
from .base import FetchResult

log = logging.getLogger(__name__)

# How long a past-due item stays visible so the dashboard can show "Overdue".
OVERDUE_GRACE = timedelta(days=7)

_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class CanvasError(RuntimeError):
    pass


class CanvasSource:
    name = CANVAS

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timezone,
        lookahead_days: int = 90,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url:
            raise CanvasError("canvas.base_url is not set in config.toml")
        self.base_url = base_url.rstrip("/")
        self.timezone = timezone
        self.lookahead_days = lookahead_days
        self._token = token
        self._timeout = timeout
        self._client = client

    # ---- http ------------------------------------------------------------

    def _open(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=self._timeout, follow_redirects=True)

    @property
    def _headers(self) -> dict[str, str]:
        """Sent per-request, not set on the client.

        Setting them on the client would skip them entirely whenever a client
        is injected, which is exactly the case the tests exercise.
        """
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _paginate(self, client: httpx.Client, url: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Follow Canvas's RFC 5988 `Link: ...; rel="next"` chain."""
        next_url: str | None = url
        next_params = params
        pages = 0
        while next_url and pages < 50:  # hard stop; a cycle here would hang the job
            response = client.get(next_url, params=next_params, headers=self._headers)
            if response.status_code == 401:
                raise CanvasError(
                    "Canvas rejected the token (401). Generate a new access token at "
                    f"{self.base_url}/profile/settings and update the CANVAS_TOKEN secret."
                )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                yield from payload
            match = _NEXT_LINK.search(response.headers.get("link", ""))
            next_url = match.group(1) if match else None
            next_params = None  # the next link already carries them
            pages += 1

    # ---- fetch -----------------------------------------------------------

    def fetch(self, now: datetime) -> FetchResult:
        result = FetchResult()
        client = self._open()
        owns_client = self._client is None
        try:
            courses = list(
                self._paginate(
                    client,
                    f"{self.base_url}/api/v1/courses",
                    {
                        "enrollment_state": "active",
                        "per_page": 100,
                        "include[]": "term",
                    },
                )
            )
            for course in courses:
                course_id = str(course.get("id", ""))
                if not course_id or course.get("access_restricted_by_date"):
                    continue
                display = course_display_name(course)
                result.courses[course_id] = display

                for assignment in self._paginate(
                    client,
                    f"{self.base_url}/api/v1/courses/{course_id}/assignments",
                    {"per_page": 100, "include[]": "submission"},
                ):
                    parsed = self._to_deadline(assignment, course_id, display, now)
                    if parsed is None:
                        continue
                    deadline, state = parsed
                    result.deadlines.append(deadline)
                    if state:
                        result.submissions[deadline.id] = state
        finally:
            if owns_client:
                client.close()

        log.info(
            "canvas: %d course(s), %d assignment(s) in window",
            len(result.courses),
            len(result.deadlines),
        )
        return result

    def _to_deadline(
        self, assignment: dict, course_id: str, course_display: str, now: datetime
    ) -> tuple[Deadline, str] | None:
        if not assignment.get("published", True):
            return None

        due_raw = assignment.get("due_at")
        if not due_raw:
            return None  # no due date means nothing to remind about

        due_at = parse_canvas_time(due_raw).astimezone(self.timezone)
        if due_at < now - OVERDUE_GRACE:
            return None
        if due_at > now + timedelta(days=self.lookahead_days):
            return None

        submission = assignment.get("submission") or {}
        state = submission.get("workflow_state", "") or ""
        if submission.get("submitted_at") and state == "unsubmitted":
            # Canvas occasionally reports a timestamp with a stale state.
            state = "submitted"

        deadline = Deadline(
            id=canvas_id(course_id, assignment["id"]),
            source=CANVAS,
            course=course_display,
            course_id=course_id,
            title=str(assignment.get("name") or "Untitled assignment").strip(),
            due_at=due_at,
            url=assignment.get("html_url"),
            private=False,
        )
        return deadline, state


def parse_canvas_time(value: str) -> datetime:
    """Canvas returns ISO 8601 in UTC with a trailing `Z`."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def course_display_name(course: dict) -> str:
    """Prefer something that reads like a course number plus a name.

    Canvas exposes both `name` and `course_code`, and which one is useful
    varies per institution, so combine them only when they add information.
    """
    name = str(course.get("name") or "").strip()
    code = str(course.get("course_code") or "").strip()
    if not name:
        return code or f"Course {course.get('id')}"
    if code and code.lower() not in name.lower():
        return f"{code} {name}"
    return name
