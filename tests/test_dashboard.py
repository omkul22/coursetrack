"""Dashboard API: reads decrypted state, writes without leaking it."""

from __future__ import annotations

import pytest

from datetime import datetime

from coursetrack.config import Secrets
from coursetrack.dashboard.app import create_app
from coursetrack.store import Store

from .conftest import make_deadline


@pytest.fixture
def client(config, key, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("COURSETRACK_KEY", key)
    return TestClient(create_app(config, Secrets()))


def test_state_is_empty_before_any_sync(client):
    payload = client.get("/api/state").json()
    assert payload["deadlines"] == []
    assert payload["health"]["ok"] is False


def test_adding_a_deadline_shows_it_with_its_real_title(client):
    response = client.post(
        "/api/deadlines",
        json={"title": "Homework 4", "course": "17-437", "due_at": "2026-10-01T23:59"},
    )
    assert response.status_code == 200

    rows = client.get("/api/state").json()["deadlines"]
    assert len(rows) == 1
    assert rows[0]["title"] == "Homework 4"
    assert rows[0]["source"] == "manual"


def test_private_titles_are_visible_locally_and_never_in_cleartext(client, config):
    """The whole reason for encrypting rather than omitting."""
    client.post(
        "/api/deadlines",
        json={
            "title": "Visa appointment",
            "course": "Personal",
            "due_at": "2026-10-01T09:00",
            "private": True,
        },
    )

    # Local dashboard: real title.
    rows = client.get("/api/state").json()["deadlines"]
    assert rows[0]["title"] == "Visa appointment"
    assert rows[0]["private"] is True

    # Nothing on disk is readable.
    assert not (config.data_dir / "deadlines.json").exists()
    for path in (config.private_path, config.payload_path):
        assert b"Visa appointment" not in path.read_bytes()


def test_bad_due_date_is_rejected_with_a_useful_message(client):
    response = client.post(
        "/api/deadlines", json={"title": "x", "due_at": "next tuesday"}
    )
    assert response.status_code == 400
    assert "ISO 8601" in response.json()["detail"]


def test_blank_title_is_rejected(client):
    response = client.post("/api/deadlines", json={"title": "   ", "due_at": "2026-10-01T09:00"})
    assert response.status_code == 400


def test_dismiss_and_restore(client):
    deadline_id = client.post(
        "/api/deadlines", json={"title": "Quiz", "due_at": "2026-10-01T09:00"}
    ).json()["id"]

    client.post(f"/api/deadlines/{deadline_id}/dismiss")
    assert client.get("/api/state").json()["deadlines"][0]["dismissed"] is True

    client.post(f"/api/deadlines/{deadline_id}/dismiss?undo=true")
    assert client.get("/api/state").json()["deadlines"][0]["dismissed"] is False


def test_deleting_a_manual_deadline(client):
    deadline_id = client.post(
        "/api/deadlines", json={"title": "Quiz", "due_at": "2026-10-01T09:00"}
    ).json()["id"]

    assert client.delete(f"/api/deadlines/{deadline_id}").status_code == 200
    assert client.get("/api/state").json()["deadlines"] == []


def test_deleting_a_canvas_deadline_is_refused(client):
    """Canvas is the source of truth for its own rows; dismiss them instead."""
    assert client.delete("/api/deadlines/canvas:1:2").status_code == 404


def test_course_toggle_round_trips(client, config, key):
    store = Store(config, key=key).load()
    store.private.course_settings["12345"] = {"enabled": True, "name": "17-437"}
    store.save([], datetime.now(config.timezone))

    client.post("/api/courses/12345/toggle", json={"enabled": False})

    payload = client.get("/api/state").json()
    assert payload["courses"][0]["enabled"] is False


def test_canvas_rows_are_rehydrated_from_the_encrypted_cache(client, config, key, now):
    """Opening the dashboard must not require a network round trip."""
    store = Store(config, key=key).load()
    store.set_canvas_cache([make_deadline(title="From Canvas", base=now)])
    store.save(None, now)

    rows = client.get("/api/state").json()["deadlines"]
    assert [r["title"] for r in rows] == ["From Canvas"]
    assert rows[0]["course_id"] == "12345"


def test_commit_messages_never_contain_a_private_title(client, config, monkeypatch):
    """Commit messages land in the public repo's history too."""
    captured: list[str] = []

    from coursetrack.dashboard import app as dashboard_app

    monkeypatch.setattr(dashboard_app.gitops, "is_repo", lambda root: True)
    monkeypatch.setattr(
        dashboard_app.gitops,
        "commit_and_push",
        lambda root, message, *paths: (captured.append(message), (True, "ok"))[1],
    )

    client.post(
        "/api/deadlines",
        json={"title": "Therapy session", "due_at": "2026-10-01T09:00", "private": True},
    )
    assert captured
    assert all("Therapy" not in m for m in captured)
    assert "private deadline" in captured[0]


# --- manual completion -----------------------------------------------------


def test_mark_done_stops_reminders_for_gradescope_work(client, config, key):
    """Canvas never sees Gradescope submissions, so it reports them unsubmitted
    forever. Marking done by hand has to suppress reminders."""
    deadline_id = client.post(
        "/api/deadlines", json={"title": "Lab 3", "due_at": "2026-10-09T23:59"}
    ).json()["id"]

    assert client.get("/api/state").json()["deadlines"][0]["done"] is False

    client.post(f"/api/deadlines/{deadline_id}/done")
    row = client.get("/api/state").json()["deadlines"][0]
    assert row["done"] is True
    assert row["manually_done"] is True

    store = Store(config, key=key).load()
    assert store.private.submission_states[deadline_id] == "submitted"


def test_mark_done_can_be_undone(client):
    deadline_id = client.post(
        "/api/deadlines", json={"title": "Lab 3", "due_at": "2026-10-09T23:59"}
    ).json()["id"]
    client.post(f"/api/deadlines/{deadline_id}/done")
    client.post(f"/api/deadlines/{deadline_id}/done?undo=true")
    assert client.get("/api/state").json()["deadlines"][0]["done"] is False


def test_manual_completion_survives_a_canvas_refetch(config, key, now):
    """A fetch replaces submission_states wholesale; the override must re-apply."""
    store = Store(config, key=key).load()
    store.mark_done("canvas:1:2")
    store.private.submission_states = {"canvas:1:2": "unsubmitted"}  # as Canvas says
    store.apply_manual_done()
    assert store.private.submission_states["canvas:1:2"] == "submitted"


# --- custom courses --------------------------------------------------------


def test_create_a_course_canvas_does_not_know_about(client):
    response = client.post("/api/courses", json={"name": "10714 Deep Learning Systems"})
    assert response.status_code == 200
    course_id = response.json()["id"]
    assert course_id.startswith("custom:")

    courses = client.get("/api/state").json()["courses"]
    assert courses[0]["name"] == "10714 Deep Learning Systems"
    assert courses[0]["custom"] is True


def test_creating_the_same_course_twice_does_not_duplicate(client):
    first = client.post("/api/courses", json={"name": "10714 DLSys"}).json()["id"]
    second = client.post("/api/courses", json={"name": "  10714 dlsys  "}).json()["id"]
    assert first == second
    assert len(client.get("/api/state").json()["courses"]) == 1


def test_blank_course_name_is_rejected(client):
    assert client.post("/api/courses", json={"name": "   "}).status_code == 400


def test_a_deadline_can_be_moved_to_another_course(client):
    course_id = client.post("/api/courses", json={"name": "10714 DLSys"}).json()["id"]
    deadline_id = client.post(
        "/api/deadlines", json={"title": "Homework 2", "due_at": "2026-10-08T23:59"}
    ).json()["id"]

    client.patch(f"/api/deadlines/{deadline_id}", json={"course_id": course_id})

    row = [r for r in client.get("/api/state").json()["deadlines"] if r["id"] == deadline_id][0]
    assert row["course_id"] == course_id
    assert row["course"] == "10714 DLSys"


def test_canvas_deadlines_cannot_be_edited(client):
    assert client.patch("/api/deadlines/canvas:1:2", json={"title": "nope"}).status_code == 404


def test_deleting_a_custom_course_keeps_its_deadlines(client):
    course_id = client.post("/api/courses", json={"name": "Temp"}).json()["id"]
    deadline_id = client.post(
        "/api/deadlines", json={"title": "Keep me", "due_at": "2026-10-08T23:59"}
    ).json()["id"]
    client.patch(f"/api/deadlines/{deadline_id}", json={"course_id": course_id})

    client.delete(f"/api/courses/{course_id}")

    state = client.get("/api/state").json()
    assert state["courses"] == []
    assert [r["title"] for r in state["deadlines"]] == ["Keep me"]
    assert state["deadlines"][0]["course"] == "Other"


def test_canvas_courses_cannot_be_deleted(client, config, key):
    store = Store(config, key=key).load()
    store.private.course_settings["55646"] = {"enabled": True, "name": "18740"}
    store.save(None, datetime.now(config.timezone))

    assert client.delete("/api/courses/55646").status_code == 400
