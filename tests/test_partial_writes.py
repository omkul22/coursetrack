"""Writes that are not a full sync must not drop Canvas deadlines.

Regression test. `coursetrack add` used to republish the public file from
manual entries alone, silently wiping every Canvas row until the next sync
repaired it — a window in which the calendar reconciler would also have
deleted the corresponding events.
"""

from __future__ import annotations


from coursetrack.cli import main
from coursetrack.store import Store

from .conftest import make_deadline, read_payload


def seed_canvas_row(config, key, now):
    """A Canvas row as a real sync would leave it: cached inside private.enc."""
    store = Store(config, key=key).load()
    store.set_canvas_cache([make_deadline(title="Canvas homework", base=now)])
    store.save(None, now)
    return store


def published_titles(config) -> set[str]:
    return {r["title"] for r in read_payload(config)["deadlines"]}


def test_all_deadlines_merges_manual_and_cached(config, key, now):
    store = seed_canvas_row(config, key, now)
    store.add_manual(make_deadline(source="manual", title="From screenshot", base=now))

    titles = {d.title for d in store.all_deadlines()}
    assert titles == {"Canvas homework", "From screenshot"}


def test_manual_rows_are_not_also_cached_as_canvas_rows(config, key, now):
    """Otherwise all_deadlines() would list every manual entry twice."""
    store = Store(config, key=key).load()
    store.add_manual(make_deadline(source="manual", title="Secret", private=True, base=now))
    store.save(None, now)

    reloaded = Store(config, key=key).load()
    assert reloaded.cached_deadlines() == []
    assert [d.title for d in reloaded.manual_deadlines()] == ["Secret"]
    assert len(reloaded.all_deadlines()) == 1


def test_cli_add_preserves_canvas_rows(config, key, now, monkeypatch):
    monkeypatch.setenv("COURSETRACK_KEY", key)
    seed_canvas_row(config, key, now)
    assert published_titles(config) == {"Canvas homework"}

    exit_code = main(
        ["--root", str(config.root), "add", "Reading response", "2026-10-02T23:59"]
    )
    assert exit_code == 0

    assert published_titles(config) == {"Canvas homework", "Reading response"}


def test_cli_set_email_preserves_canvas_rows(config, key, now, monkeypatch):
    monkeypatch.setenv("COURSETRACK_KEY", key)
    seed_canvas_row(config, key, now)

    assert main(["--root", str(config.root), "set-email", "me@example.com"]) == 0
    assert published_titles(config) == {"Canvas homework"}


def test_cli_remove_preserves_canvas_rows(config, key, now, monkeypatch):
    monkeypatch.setenv("COURSETRACK_KEY", key)
    seed_canvas_row(config, key, now)
    main(["--root", str(config.root), "add", "Temporary", "2026-10-02T23:59"])

    store = Store(config, key=key).load()
    manual_id = store.private.manual_entries[0]["id"]

    assert main(["--root", str(config.root), "remove", manual_id]) == 0
    assert published_titles(config) == {"Canvas homework"}


def test_dashboard_add_preserves_canvas_rows(config, key, now, monkeypatch):
    from fastapi.testclient import TestClient

    from coursetrack.config import Secrets
    from coursetrack.dashboard.app import create_app

    monkeypatch.setenv("COURSETRACK_KEY", key)
    seed_canvas_row(config, key, now)

    client = TestClient(create_app(config, Secrets()))
    client.post("/api/deadlines", json={"title": "Added in UI", "due_at": "2026-10-02T23:59"})

    assert published_titles(config) == {"Canvas homework", "Added in UI"}
