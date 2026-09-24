"""Threshold crossing, idempotency, and the DST-safe daily guards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from coursetrack.reminders import (
    brief_window,
    due_reminders,
    record,
    should_heartbeat,
    should_send_brief,
)

from .conftest import ET, make_deadline

THRESHOLDS = (timedelta(hours=72), timedelta(hours=12))


def _fire(deadlines, now, ledger=None, **kwargs):
    ledger = {} if ledger is None else ledger
    return due_reminders(deadlines, now, thresholds=THRESHOLDS, ledger=ledger, **kwargs)


# --- threshold crossing ----------------------------------------------------


def test_nothing_fires_well_before_the_first_threshold(now):
    deadline = make_deadline(hours_out=100, base=now)
    assert _fire([deadline], now) == []


def test_72h_threshold_fires_at_the_boundary(now):
    deadline = make_deadline(hours_out=72, base=now)
    pending = _fire([deadline], now)
    assert len(pending) == 1
    assert pending[0].tripped == (72,)
    assert pending[0].label_hours == 72


def test_12h_threshold_fires_after_72h_is_already_recorded(now):
    deadline = make_deadline(hours_out=12, base=now)
    ledger = {deadline.id: {"72": "2026-09-19T09:00:00-04:00"}}
    pending = _fire([deadline], now, ledger)
    assert pending[0].tripped == (12,)
    assert pending[0].label_hours == 12


def test_deadline_added_inside_the_12h_window_collapses_to_one_entry(now):
    """Both thresholds trip at once; the email must say 12 hours, not 3 days."""
    deadline = make_deadline(hours_out=5, base=now)
    pending = _fire([deadline], now)
    assert len(pending) == 1, "should be one digest row, not two"
    assert set(pending[0].tripped) == {72, 12}
    assert pending[0].label_hours == 12


def test_past_due_never_reminds(now):
    """A 3-day warning for something already overdue is noise, not a reminder."""
    assert _fire([make_deadline(hours_out=-1, base=now)], now) == []
    assert _fire([make_deadline(hours_out=0, base=now)], now) == []


def test_results_are_sorted_by_due_date(now):
    later = make_deadline(title="later", hours_out=70, base=now)
    sooner = make_deadline(title="sooner", hours_out=3, base=now)
    pending = _fire([later, sooner], now)
    assert [p.deadline.title for p in pending] == ["sooner", "later"]


# --- idempotency -----------------------------------------------------------


def test_running_twice_sends_exactly_once(now):
    deadline = make_deadline(hours_out=71, base=now)
    ledger: dict = {}

    first = _fire([deadline], now, ledger)
    assert len(first) == 1
    record(ledger, first, now)

    # Same tick re-run, and an hour later — neither may re-send.
    assert _fire([deadline], now, ledger) == []
    assert _fire([deadline], now + timedelta(hours=1), ledger) == []


def test_record_marks_every_tripped_threshold(now):
    deadline = make_deadline(hours_out=5, base=now)
    ledger: dict = {}
    record(ledger, _fire([deadline], now, ledger), now)
    assert set(ledger[deadline.id]) == {"72", "12"}


def test_a_full_lifecycle_sends_exactly_two_emails(now):
    """Tick hourly for five days across one deadline; expect 72h and 12h only."""
    deadline = make_deadline(hours_out=120, base=now)
    ledger: dict = {}
    sends = []

    for hour in range(0, 130):
        moment = now + timedelta(hours=hour)
        pending = _fire([deadline], moment, ledger)
        if pending:
            sends.append(pending[0].label_hours)
            record(ledger, pending, moment)

    assert sends == [72, 12]


# --- suppression -----------------------------------------------------------


def test_submitted_assignments_are_suppressed(now):
    deadline = make_deadline(hours_out=10, base=now)
    for state in ("submitted", "graded", "pending_review"):
        assert _fire([deadline], now, submissions={deadline.id: state}) == []
    assert len(_fire([deadline], now, submissions={deadline.id: "unsubmitted"})) == 1


def test_skip_submitted_can_be_turned_off(now):
    deadline = make_deadline(hours_out=10, base=now)
    pending = _fire(
        [deadline], now, submissions={deadline.id: "submitted"}, skip_submitted=False
    )
    assert len(pending) == 1


def test_dismissed_and_disabled_course_are_suppressed(now):
    deadline = make_deadline(hours_out=10, base=now)
    assert _fire([deadline], now, dismissed=[deadline.id]) == []
    assert _fire([deadline], now, course_enabled=lambda cid: False) == []
    assert len(_fire([deadline], now, course_enabled=lambda cid: True)) == 1


# --- daily guards, including DST ------------------------------------------


def test_brief_does_not_fire_before_the_hour(now):
    early = now.replace(hour=6)
    assert not should_send_brief(early, "", brief_hour=7)


def test_brief_fires_once_per_day(now):
    morning = now.replace(hour=7)
    assert should_send_brief(morning, "", brief_hour=7)
    assert not should_send_brief(morning, morning.date().isoformat(), brief_hour=7)


def test_delayed_run_still_sends_the_brief(now):
    """Actions cron can skip the 7am slot entirely; `>=` recovers at 9am."""
    assert should_send_brief(now.replace(hour=9), "", brief_hour=7)
    assert should_send_brief(now.replace(hour=23), "", brief_hour=7)


def test_exactly_one_brief_per_day_across_the_dst_fallback():
    """1 Nov 2026 is the US DST fallback: 1am-2am ET happens twice.

    Ticking in UTC and converting to ET is what the real job does, so this
    exercises the actual code path rather than a synthetic local clock.
    """
    start = datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc)
    last_brief = ""
    briefs: list[str] = []

    for hour in range(0, 72):
        now = (start + timedelta(hours=hour)).astimezone(ET)
        if should_send_brief(now, last_brief, brief_hour=7):
            last_brief = now.date().isoformat()
            briefs.append(last_brief)

    assert briefs == ["2026-10-31", "2026-11-01", "2026-11-02"]
    assert len(briefs) == len(set(briefs)), "a repeated local hour must not double-send"


def test_exactly_one_brief_per_day_across_the_dst_spring_forward():
    """8 Mar 2026: 2am-3am ET does not exist."""
    start = datetime(2026, 3, 7, 5, 0, tzinfo=timezone.utc)
    last_brief = ""
    briefs: list[str] = []

    for hour in range(0, 72):
        now = (start + timedelta(hours=hour)).astimezone(ET)
        if should_send_brief(now, last_brief, brief_hour=7):
            last_brief = now.date().isoformat()
            briefs.append(last_brief)

    assert briefs == ["2026-03-07", "2026-03-08", "2026-03-09"]


def test_heartbeat_uses_the_same_guard(now):
    assert should_heartbeat(now.replace(hour=3), "", heartbeat_hour=3)
    assert not should_heartbeat(now.replace(hour=2), "", heartbeat_hour=3)
    assert not should_heartbeat(
        now.replace(hour=5), now.date().isoformat(), heartbeat_hour=3
    )


# --- brief contents --------------------------------------------------------


def test_brief_window_covers_seven_days_and_excludes_the_rest(now):
    inside = make_deadline(title="inside", hours_out=48, base=now)
    edge = make_deadline(title="edge", hours_out=24 * 7, base=now)
    outside = make_deadline(title="outside", hours_out=24 * 8, base=now)
    past = make_deadline(title="past", hours_out=-5, base=now)

    rows = brief_window([outside, inside, past, edge], now, days=7)
    assert [d.title for d in rows] == ["inside", "edge"]
