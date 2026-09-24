"""End-to-end tick behaviour with Canvas, calendar and SMTP stubbed out."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from coursetrack import engine
from coursetrack.config import Secrets
from coursetrack.sources.base import FetchResult
from coursetrack.store import Store

from .conftest import make_deadline


@dataclass
class SentMessage:
    subject: str
    text: str


@dataclass
class FakeMailer:
    outbox: list = field(default_factory=list)
    fail: bool = False

    def send(self, subject, text, html=None):
        if self.fail:
            from coursetrack.notify.email import EmailError

            raise EmailError("simulated SMTP outage")
        self.outbox.append(SentMessage(subject, text))
        return True


@pytest.fixture
def secrets():
    return Secrets(
        canvas_token="canvas-tok",
        gmail_address="me@example.com",
        gmail_app_password="app-pass",
    )


@pytest.fixture
def wire(monkeypatch):
    """Point the engine at fakes and hand back the mailer so tests can inspect it."""
    mailer = FakeMailer()

    def install(deadlines, submissions=None, courses=None):
        result = FetchResult(
            deadlines=list(deadlines),
            submissions=dict(submissions or {}),
            courses=dict(courses or {}),
        )

        class FakeSource:
            def __init__(self, *a, **k):
                pass

            def fetch(self, now):
                return result

        monkeypatch.setattr(engine, "CanvasSource", FakeSource)
        monkeypatch.setattr(engine, "build_mailer", lambda *a, **k: mailer)
        return mailer

    return install


def tick(config, secrets, store, now, **kwargs):
    kwargs.setdefault("skip_calendar", True)
    return engine.run_tick(config, secrets, store, now, **kwargs)


def suppress_brief(store, now):
    """Mark today's brief as already sent.

    A first run after 7am legitimately sends both a threshold digest and the
    daily brief. Tests about the digest say so explicitly rather than relying
    on a clock that happens to be before 7am.
    """
    store.private.last_brief_date = now.date().isoformat()


# --- the happy path --------------------------------------------------------


def test_tick_sends_one_digest_and_records_the_ledger(config, key, secrets, wire, now):
    deadline = make_deadline(hours_out=70, base=now)
    mailer = wire([deadline])
    store = Store(config, key=key).load()
    store.private.recipient_email = "me@example.com"
    suppress_brief(store, now)

    report = tick(config, secrets, store, now)

    assert report.reminder_sent
    assert len(mailer.outbox) == 1
    assert "Homework 3" in mailer.outbox[0].subject
    assert store.private.reminder_ledger[deadline.id] == {"72": now.isoformat()}


def test_two_deadlines_produce_one_digest_not_two_emails(config, key, secrets, wire, now):
    from dataclasses import replace

    a = make_deadline(title="Homework 3", hours_out=70, base=now)
    b = replace(make_deadline(title="Quiz 2", hours_out=11, base=now), id="canvas:12345:222")
    mailer = wire([a, b])
    store = Store(config, key=key).load()
    store.private.recipient_email = "me@example.com"
    suppress_brief(store, now)

    tick(config, secrets, store, now)

    assert len(mailer.outbox) == 1
    assert mailer.outbox[0].subject == "2 deadlines approaching"
    assert "Homework 3" in mailer.outbox[0].text
    assert "Quiz 2" in mailer.outbox[0].text


def test_repeated_ticks_do_not_resend(config, key, secrets, wire, now):
    deadline = make_deadline(hours_out=70, base=now)
    mailer = wire([deadline])
    store = Store(config, key=key).load()
    store.private.recipient_email = "me@example.com"

    for hour in range(6):
        tick(config, secrets, store, now + timedelta(hours=hour))

    reminder_subjects = [m for m in mailer.outbox if "approaching" in m.subject or "Due in" in m.subject]
    assert len(reminder_subjects) == 1


# --- failure handling ------------------------------------------------------


def test_a_failed_send_does_not_mark_the_ledger(config, key, secrets, wire, now):
    """Otherwise an SMTP blip silently eats the only warning you were going to get."""
    from coursetrack.notify.email import EmailError

    deadline = make_deadline(hours_out=70, base=now)
    mailer = wire([deadline])
    mailer.fail = True
    store = Store(config, key=key).load()
    store.private.recipient_email = "me@example.com"

    with pytest.raises(EmailError):
        tick(config, secrets, store, now)

    assert deadline.id not in store.private.reminder_ledger

    # Next tick, with SMTP healthy again, still delivers it.
    mailer.fail = False
    tick(config, secrets, store, now + timedelta(hours=1))
    assert len(mailer.outbox) >= 1
    assert "72" in store.private.reminder_ledger[deadline.id]


def test_a_calendar_outage_does_not_block_reminders(config, key, secrets, wire, now, monkeypatch):
    """Email is the part that actually gets work submitted; it must survive."""
    deadline = make_deadline(hours_out=70, base=now)
    mailer = wire([deadline])

    def explode(*args, **kwargs):
        raise RuntimeError("calendar is down")

    monkeypatch.setattr(engine, "_sync_calendar", explode)

    store = Store(config, key=key).load()
    store.private.recipient_email = "me@example.com"
    suppress_brief(store, now)

    report = engine.run_tick(
        config,
        Secrets(
            canvas_token="t",
            google_service_account_json="{}",
            google_calendar_id="cal@group.calendar.google.com",
            gmail_address="me@example.com",
            gmail_app_password="p",
        ),
        store,
        now,
    )

    assert report.errors and "calendar" in report.errors[0]
    assert report.reminder_sent
    assert len(mailer.outbox) == 1


def test_transient_canvas_failure_is_retried(config, key, secrets, monkeypatch, now):
    attempts = {"n": 0}

    class FlakySource:
        def __init__(self, *a, **k):
            pass

        def fetch(self, now):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("connection reset")
            return FetchResult(deadlines=[make_deadline(hours_out=200, base=now)])

    monkeypatch.setattr(engine, "CanvasSource", FlakySource)
    monkeypatch.setattr(engine, "FETCH_BACKOFF_SECONDS", 0)

    store = Store(config, key=key).load()
    report = tick(config, secrets, store, now, skip_email=True)

    assert attempts["n"] == 3
    assert len(report.deadlines) == 1


def test_persistent_canvas_failure_raises_so_the_workflow_alerts(config, key, secrets, monkeypatch, now):
    class DeadSource:
        def __init__(self, *a, **k):
            pass

        def fetch(self, now):
            raise RuntimeError("canvas is down")

    monkeypatch.setattr(engine, "CanvasSource", DeadSource)
    monkeypatch.setattr(engine, "FETCH_BACKOFF_SECONDS", 0)

    store = Store(config, key=key).load()
    with pytest.raises(RuntimeError, match="canvas is down"):
        tick(config, secrets, store, now, skip_email=True)


# --- brief, heartbeat, state ----------------------------------------------


def test_brief_is_sent_once_per_day(config, key, secrets, wire, now):
    mailer = wire([make_deadline(hours_out=200, base=now)])
    store = Store(config, key=key).load()
    store.private.recipient_email = "me@example.com"

    morning = now.replace(hour=7)
    tick(config, secrets, store, morning)
    tick(config, secrets, store, morning.replace(hour=8))
    tick(config, secrets, store, morning.replace(hour=15))

    briefs = [m for m in mailer.outbox if "week" in m.subject.lower()]
    assert len(briefs) == 1
    assert store.private.last_brief_date == morning.date().isoformat()


def test_manual_and_canvas_deadlines_are_merged(config, key, secrets, wire, now):
    wire([make_deadline(title="From Canvas", hours_out=200, base=now)])
    store = Store(config, key=key).load()
    store.add_manual(make_deadline(source="manual", title="From screenshot", hours_out=150, base=now))

    report = tick(config, secrets, store, now, skip_email=True)

    assert {d.title for d in report.deadlines} == {"From Canvas", "From screenshot"}


def test_disabled_courses_are_excluded_from_reminders(config, key, secrets, wire, now):
    deadline = make_deadline(hours_out=70, base=now)
    mailer = wire([deadline], courses={"12345": "17-437"})
    store = Store(config, key=key).load()
    store.private.recipient_email = "me@example.com"
    store.set_course_enabled("12345", False)

    report = tick(config, secrets, store, now)

    assert report.reminders == []
    assert not [m for m in mailer.outbox if "Due in" in m.subject]


def test_courses_are_discovered_and_default_to_enabled(config, key, secrets, wire, now):
    wire([], courses={"999": "15-122 Imperative Computation"})
    store = Store(config, key=key).load()

    tick(config, secrets, store, now, skip_email=True)

    assert store.private.course_settings["999"]["name"] == "15-122 Imperative Computation"
    assert store.course_enabled("999") is True


def test_dry_run_writes_nothing(config, key, secrets, wire, now):
    wire([make_deadline(hours_out=70, base=now)])
    store = Store(config, key=key).load()
    store.private.recipient_email = "me@example.com"

    tick(config, secrets, store, now, dry_run=True)

    assert not config.payload_path.exists()
    assert not config.private_path.exists()


def test_state_round_trips_between_ticks(config, key, secrets, wire, now):
    deadline = make_deadline(hours_out=70, base=now)
    wire([deadline])

    store = Store(config, key=key).load()
    store.private.recipient_email = "me@example.com"
    tick(config, secrets, store, now)

    reloaded = Store(config, key=key).load()
    assert "72" in reloaded.private.reminder_ledger[deadline.id]
