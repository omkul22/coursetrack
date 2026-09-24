"""Calendar reconciliation: create, patch, prune, and duplicate-proofing."""

from __future__ import annotations

from datetime import timedelta

from coursetrack.google.calendar import (
    DONE_PREFIX,
    ID_PROP,
    MANAGED_FLAG,
    event_body,
    needs_update,
    reconcile,
)

from .conftest import ET, make_deadline

TZ = "America/New_York"


class FakeCalendar:
    """Stands in for the Google API, keyed the way the real one is."""

    def __init__(self, events: list[dict] | None = None) -> None:
        self.events = {e["id"]: e for e in (events or [])}
        self.inserted: list[dict] = []
        self.patched: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self._next = 1000

    def managed_events(self, calendar_id, time_min, time_max):
        return {
            e["extendedProperties"]["private"][ID_PROP]: e for e in self.events.values()
        }

    def insert(self, calendar_id, body):
        self._next += 1
        event = {**body, "id": f"evt{self._next}"}
        self.events[event["id"]] = event
        self.inserted.append(body)
        return event

    def patch(self, calendar_id, event_id, body):
        self.events[event_id] = {**self.events[event_id], **body}
        self.patched.append((event_id, body))
        return self.events[event_id]

    def delete(self, calendar_id, event_id):
        self.events.pop(event_id, None)
        self.deleted.append(event_id)


def stored(deadline, **overrides):
    """An event as Google would have returned it for this deadline."""
    body = event_body(deadline, block_minutes=30, timezone_name=TZ)
    body.update(overrides)
    return {**body, "id": f"evt-{deadline.id}"}


def run(client, deadlines, now, **kwargs):
    return reconcile(
        client, "cal1", deadlines, now=now, block_minutes=30, timezone_name=TZ, **kwargs
    )


# --- event shape -----------------------------------------------------------


def test_block_ends_exactly_at_the_deadline(now):
    deadline = make_deadline(hours_out=48, base=now)
    body = event_body(deadline, block_minutes=30, timezone_name=TZ)

    assert body["end"]["dateTime"] == deadline.due_at.isoformat()
    assert body["start"]["dateTime"] == (deadline.due_at - timedelta(minutes=30)).isoformat()
    assert body["summary"] == f"DUE: {deadline.title} — {deadline.course}"


def test_events_carry_both_idempotency_markers(now):
    deadline = make_deadline(base=now)
    props = event_body(deadline, block_minutes=30, timezone_name=TZ)["extendedProperties"]["private"]
    assert props[MANAGED_FLAG] == "1"
    assert props[ID_PROP] == deadline.id


def test_submitted_assignments_get_a_checkmark(now):
    deadline = make_deadline(base=now)
    body = event_body(deadline, block_minutes=30, timezone_name=TZ, submitted=True)
    assert body["summary"].startswith(DONE_PREFIX)


def test_private_deadlines_keep_their_real_title_on_the_calendar(now):
    """The payoff for encrypting rather than omitting: your own calendar is useful."""
    deadline = make_deadline(
        source="manual", private=True, course="Immigration", title="OPT paperwork", base=now
    )
    body = event_body(deadline, block_minutes=30, timezone_name=TZ)
    assert "OPT paperwork" in body["summary"]
    assert "Immigration" in body["summary"]


# --- reconcile -------------------------------------------------------------


def test_missing_events_are_created(now):
    client = FakeCalendar()
    ids: dict = {}
    plan = run(client, [make_deadline(base=now)], now, event_ids=ids)

    assert len(plan.created) == 1
    assert len(client.inserted) == 1
    assert ids  # the id cache is populated for next time


def test_unchanged_events_are_left_alone(now):
    """Google echoes back extra server fields; only owned fields may be diffed."""
    deadline = make_deadline(base=now)
    existing = stored(deadline)
    existing.update({"etag": '"abc"', "iCalUID": "x@google.com", "status": "confirmed"})

    client = FakeCalendar([existing])
    plan = run(client, [deadline], now, event_ids={})

    assert plan.empty
    assert not client.patched and not client.inserted and not client.deleted


def test_a_moved_deadline_patches_the_event(now):
    original = make_deadline(hours_out=48, base=now)
    client = FakeCalendar([stored(original)])

    from dataclasses import replace

    moved = replace(original, due_at=original.due_at + timedelta(days=2))
    plan = run(client, [moved], now, event_ids={})

    assert plan.updated == [original.id]
    assert client.patched[0][1]["end"]["dateTime"] == moved.due_at.isoformat()


def test_a_renamed_assignment_patches_the_event(now):
    from dataclasses import replace

    original = make_deadline(base=now)
    client = FakeCalendar([stored(original)])
    plan = run(client, [replace(original, title="Homework 3 (revised)")], now, event_ids={})
    assert plan.updated == [original.id]


def test_deleted_assignments_are_pruned(now):
    gone = make_deadline(title="Cancelled quiz", base=now)
    client = FakeCalendar([stored(gone)])
    ids = {gone.id: f"evt-{gone.id}"}

    plan = run(client, [], now, event_ids=ids)

    assert plan.deleted == [gone.id]
    assert client.deleted == [f"evt-{gone.id}"]
    assert gone.id not in ids


def test_dismissed_deadlines_are_removed_from_the_calendar(now):
    deadline = make_deadline(base=now)
    client = FakeCalendar([stored(deadline)])
    plan = run(client, [deadline], now, event_ids={}, dismissed=[deadline.id])
    assert plan.deleted == [deadline.id]


def test_lost_state_does_not_duplicate_events(now):
    """The whole point of stamping coursetrack_id onto the event itself.

    Simulates `private.enc` being lost: the id cache starts empty, but the
    events still identify themselves, so reconcile adopts them.
    """
    deadlines = [make_deadline(base=now), make_deadline(title="Quiz 2", base=now)]
    # Give them distinct ids the way Canvas would.
    from dataclasses import replace

    deadlines[1] = replace(deadlines[1], id="canvas:12345:11111")

    client = FakeCalendar([stored(d) for d in deadlines])
    recovered_ids: dict = {}

    plan = run(client, deadlines, now, event_ids=recovered_ids)

    assert plan.empty, "existing events must be adopted, not recreated"
    assert not client.inserted
    assert len(recovered_ids) == 2, "the id cache should be rebuilt from the events"


def test_submission_state_flows_into_the_summary(now):
    deadline = make_deadline(base=now)
    client = FakeCalendar([stored(deadline)])
    plan = run(client, [deadline], now, event_ids={}, submissions={deadline.id: "graded"})

    assert plan.updated == [deadline.id]
    assert client.patched[0][1]["summary"].startswith(DONE_PREFIX)


def test_dry_run_changes_nothing(now):
    deadline = make_deadline(base=now)
    client = FakeCalendar()
    plan = run(client, [deadline], now, event_ids={}, dry_run=True)

    assert plan.created == [deadline.id]
    assert not client.inserted, "dry run must not write"


# --- diffing ---------------------------------------------------------------


def test_needs_update_ignores_equivalent_offset_spellings(now):
    deadline = make_deadline(base=now)
    desired = event_body(deadline, block_minutes=30, timezone_name=TZ)
    existing = {
        "summary": desired["summary"],
        "description": desired["description"],
        "start": {"dateTime": desired["start"]["dateTime"].replace("-04:00", "-04:00")},
        "end": {"dateTime": deadline.due_at.astimezone(ET).isoformat()},
    }
    assert not needs_update(existing, desired)


# --- past deadlines --------------------------------------------------------


def test_past_deadlines_do_not_get_new_events(now):
    """They would be deleted again within days as they age out of the window."""
    client = FakeCalendar()
    plan = run(client, [make_deadline(title="Overdue", hours_out=-30, base=now)], now, event_ids={})
    assert plan.created == []
    assert not client.inserted


def test_an_existing_event_survives_its_deadline_passing(now):
    """Once created, the event stays as a record of what was due."""
    deadline = make_deadline(hours_out=-2, base=now)
    client = FakeCalendar([stored(deadline)])
    plan = run(client, [deadline], now, event_ids={})
    assert plan.deleted == []
    assert not client.deleted


def test_future_deadlines_are_still_created_alongside_past_ones(now):
    from dataclasses import replace

    past = make_deadline(title="Overdue", hours_out=-30, base=now)
    future = replace(make_deadline(title="Upcoming", hours_out=30, base=now), id="canvas:1:999")
    client = FakeCalendar()
    plan = run(client, [past, future], now, event_ids={})
    assert plan.created == [future.id]
    assert len(client.inserted) == 1
