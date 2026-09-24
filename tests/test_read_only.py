"""Canvas access is strictly read-only.

A hard requirement: this tool observes your coursework, it never modifies it.
It must never submit, unsubmit, comment, change a due date, or touch a grade.

`test_canvas.py` covers parsing. This file covers the *method* of every request
that leaves the process, so a future change that adds a write is caught here
rather than in production against a real gradebook.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from coursetrack.sources.canvas import CanvasSource

from .conftest import ET

BASE = "https://canvas.example.edu"
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=ET)

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class MethodRecorder:
    """Records the method of every request and serves plausible Canvas data."""

    def __init__(self) -> None:
        self.methods: list[str] = []
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.methods.append(request.method)
        self.urls.append(str(request.url))

        if "assignments" in str(request.url):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 55,
                        "name": "Homework 3",
                        "due_at": "2026-09-26T03:59:00Z",
                        "published": True,
                        "html_url": f"{BASE}/courses/1/assignments/55",
                        "submission": {"workflow_state": "unsubmitted", "submitted_at": None},
                    }
                ],
            )
        return httpx.Response(200, json=[{"id": 1, "name": "Web Apps", "course_code": "17-437"}])


def test_a_full_canvas_fetch_issues_only_get_requests():
    recorder = MethodRecorder()
    source = CanvasSource(
        BASE, "tok", timezone=ET,
        client=httpx.Client(transport=httpx.MockTransport(recorder)),
    )
    source.fetch(NOW)

    assert recorder.methods, "the fetch should have made at least one request"
    assert set(recorder.methods) == {"GET"}, (
        f"Canvas must be read-only, but these methods were used: "
        f"{sorted(set(recorder.methods) - {'GET'})}"
    )


def test_no_canvas_url_targets_a_mutating_endpoint():
    """Belt and braces: even a GET must not hit a side-effecting path."""
    recorder = MethodRecorder()
    source = CanvasSource(
        BASE, "tok", timezone=ET,
        client=httpx.Client(transport=httpx.MockTransport(recorder)),
    )
    source.fetch(NOW)

    forbidden = ("/submissions", "/grade", "/submit", "/files", "/comments")
    for url in recorder.urls:
        for fragment in forbidden:
            assert fragment not in url, f"request touched a mutating path: {url}"


def test_the_canvas_module_contains_no_write_verbs():
    """Structural guard on the source file itself.

    Cheap, and it fails the moment someone adds `client.post(...)` to the
    Canvas layer even if no test happens to exercise that code path.
    """
    import inspect

    from coursetrack.sources import canvas

    source_code = inspect.getsource(canvas)
    for verb in ("client.post(", "client.put(", "client.patch(", "client.delete("):
        assert verb not in source_code, f"Canvas layer must not contain {verb}"


def test_no_gradescope_module_exists():
    """Gradescope is deferred; nothing should be reaching it yet."""
    with pytest.raises(ImportError):
        import coursetrack.sources.gradescope  # noqa: F401


def test_calendar_writes_are_confined_to_the_managed_calendar():
    """Calendar IS read-write by design — but only on its own calendar.

    Every mutating call must carry an explicit calendar id in the path, never
    `primary`, so the tool cannot touch your main calendar.
    """
    import inspect

    from coursetrack.google import calendar

    source_code = inspect.getsource(calendar)
    assert "primary" not in source_code, (
        "the calendar layer must never reference the `primary` calendar"
    )
    for path in ("/calendars/{calendar_id}/events",):
        assert path in source_code
