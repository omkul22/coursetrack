"""Canvas parsing, pagination and windowing, against a mock transport."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from coursetrack.sources.canvas import (
    CanvasError,
    CanvasSource,
    course_display_name,
    parse_canvas_time,
)

from .conftest import ET

BASE = "https://canvas.example.edu"
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=ET)


def _assignment(aid: int, name: str, due_iso: str | None, **extra):
    body = {
        "id": aid,
        "name": name,
        "due_at": due_iso,
        "published": True,
        "html_url": f"{BASE}/courses/1/assignments/{aid}",
        "submission": {"workflow_state": "unsubmitted", "submitted_at": None},
    }
    body.update(extra)
    return body


def build_source(routes, **kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        for predicate, response in routes:
            if predicate(request):
                return response
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CanvasSource(BASE, "tok", timezone=ET, client=client, **kwargs)


def simple_routes(courses, assignments):
    return [
        (lambda r: "/courses" in str(r.url) and "assignments" not in str(r.url),
         httpx.Response(200, json=courses)),
        (lambda r: "assignments" in str(r.url),
         httpx.Response(200, json=assignments)),
    ]


# --- parsing ---------------------------------------------------------------


def test_utc_due_dates_are_converted_to_local():
    """Canvas speaks UTC with a Z; everything downstream assumes local."""
    parsed = parse_canvas_time("2026-09-26T03:59:00Z").astimezone(ET)
    assert parsed.isoformat() == "2026-09-25T23:59:00-04:00"


def test_course_display_name_combines_code_and_name_only_when_useful():
    assert course_display_name({"name": "Web Apps", "course_code": "17-437"}) == "17-437 Web Apps"
    # Already contains the code — do not repeat it.
    assert course_display_name({"name": "17-437 Web Apps", "course_code": "17-437"}) == "17-437 Web Apps"
    assert course_display_name({"name": "", "course_code": "17-437"}) == "17-437"
    assert course_display_name({"id": 9}) == "Course 9"


# --- fetching --------------------------------------------------------------


def test_fetch_builds_stable_ids_and_local_times():
    source = build_source(
        simple_routes(
            [{"id": 1, "name": "Web Apps", "course_code": "17-437"}],
            [_assignment(55, "Homework 3", "2026-09-26T03:59:00Z")],
        )
    )
    result = source.fetch(NOW)

    assert len(result.deadlines) == 1
    deadline = result.deadlines[0]
    assert deadline.id == "canvas:1:55"
    assert deadline.course == "17-437 Web Apps"
    assert deadline.due_at.isoformat() == "2026-09-25T23:59:00-04:00"
    assert deadline.private is False
    assert result.courses == {"1": "17-437 Web Apps"}


def test_ids_are_stable_across_runs():
    """Calendar idempotency depends entirely on this."""
    routes = simple_routes(
        [{"id": 1, "name": "Web Apps"}],
        [_assignment(55, "Homework 3", "2026-09-26T03:59:00Z")],
    )
    first = build_source(routes).fetch(NOW).deadlines[0].id
    second = build_source(routes).fetch(NOW + timedelta(hours=3)).deadlines[0].id
    assert first == second


def test_assignments_without_a_due_date_are_skipped():
    source = build_source(
        simple_routes([{"id": 1, "name": "Web Apps"}], [_assignment(55, "Reading", None)])
    )
    assert source.fetch(NOW).deadlines == []


def test_unpublished_assignments_are_skipped():
    source = build_source(
        simple_routes(
            [{"id": 1, "name": "Web Apps"}],
            [_assignment(55, "Draft", "2026-09-26T03:59:00Z", published=False)],
        )
    )
    assert source.fetch(NOW).deadlines == []


def test_window_excludes_far_future_and_ancient_past():
    source = build_source(
        simple_routes(
            [{"id": 1, "name": "Web Apps"}],
            [
                _assignment(1, "next year", "2027-09-26T03:59:00Z"),
                _assignment(2, "last month", "2026-08-01T03:59:00Z"),
                _assignment(3, "recent overdue", "2026-09-20T03:59:00Z"),
                _assignment(4, "upcoming", "2026-09-26T03:59:00Z"),
            ],
        ),
        lookahead_days=90,
    )
    titles = {d.title for d in source.fetch(NOW).deadlines}
    # Recently overdue stays visible so the dashboard can show it.
    assert titles == {"recent overdue", "upcoming"}


def test_submission_state_is_captured():
    source = build_source(
        simple_routes(
            [{"id": 1, "name": "Web Apps"}],
            [
                _assignment(
                    55, "Homework 3", "2026-09-26T03:59:00Z",
                    submission={"workflow_state": "graded", "submitted_at": "2026-09-20T01:00:00Z"},
                )
            ],
        )
    )
    result = source.fetch(NOW)
    assert result.submissions["canvas:1:55"] == "graded"


def test_stale_unsubmitted_state_with_a_timestamp_is_corrected():
    source = build_source(
        simple_routes(
            [{"id": 1, "name": "Web Apps"}],
            [
                _assignment(
                    55, "Homework 3", "2026-09-26T03:59:00Z",
                    submission={"workflow_state": "unsubmitted", "submitted_at": "2026-09-20T01:00:00Z"},
                )
            ],
        )
    )
    assert source.fetch(NOW).submissions["canvas:1:55"] == "submitted"


# --- pagination ------------------------------------------------------------


def test_link_header_pagination_is_followed():
    page_two = f"{BASE}/api/v1/courses/1/assignments?page=2"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "assignments" not in url:
            return httpx.Response(200, json=[{"id": 1, "name": "Web Apps"}])
        if "page=2" in url:
            return httpx.Response(200, json=[_assignment(2, "Second", "2026-09-27T03:59:00Z")])
        return httpx.Response(
            200,
            json=[_assignment(1, "First", "2026-09-26T03:59:00Z")],
            headers={"Link": f'<{page_two}>; rel="next"'},
        )

    source = CanvasSource(
        BASE, "tok", timezone=ET, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    titles = {d.title for d in source.fetch(NOW).deadlines}
    assert titles == {"First", "Second"}
    assert any("page=2" in c for c in calls)


def test_expired_token_gives_an_actionable_error():
    def handler(request):
        return httpx.Response(401, json={"errors": [{"message": "Invalid access token."}]})

    source = CanvasSource(
        BASE, "bad", timezone=ET, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(CanvasError, match="401"):
        source.fetch(NOW)


def test_token_is_never_placed_in_the_query_string():
    """A token in a URL ends up in logs, proxies and tracebacks."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer super-secret"
        return httpx.Response(200, json=[])

    source = CanvasSource(
        BASE, "super-secret", timezone=ET,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    source.fetch(NOW)
    assert seen and all("super-secret" not in url for url in seen)
