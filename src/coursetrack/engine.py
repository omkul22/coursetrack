"""Orchestration: gather, reconcile, remind, persist.

`run_tick` is the single entrypoint the scheduled job calls, and the same one
the dashboard's "Sync now" button calls. There is deliberately no separate
"nightly" path — a tick does the full job every time, because a full Canvas
fetch is a handful of cheap requests and one code path cannot drift from
another.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config, Secrets
from .google.calendar import CalendarClient, ReconcilePlan, reconcile
from .google.service_account import ServiceAccount, TokenProvider
from .models import Deadline
from .notify.email import build_mailer
from .notify.templates import render_brief, render_digest
from .reminders import (
    brief_window,
    due_reminders,
    record,
    should_heartbeat,
    should_send_brief,
)
from .sources.base import FetchResult
from .sources.canvas import CanvasSource
from .store import SaveResult, Store

log = logging.getLogger(__name__)

FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 4


@dataclass
class TickReport:
    now: datetime
    deadlines: list[Deadline] = field(default_factory=list)
    reminders: list = field(default_factory=list)
    reminder_sent: bool = False
    brief_sent: bool = False
    heartbeat: bool = False
    calendar: ReconcilePlan | None = None
    save: SaveResult | None = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"{len(self.deadlines)} deadline(s)"]
        if self.calendar:
            bits.append(f"calendar: {self.calendar.summary()}")
        bits.append(
            f"reminders: {len(self.reminders)} pending"
            + (" (sent)" if self.reminder_sent else " (not sent)")
        )
        if self.brief_sent:
            bits.append("brief sent")
        if self.heartbeat:
            bits.append("heartbeat")
        if self.save:
            bits.append(
                f"state: payload={'changed' if self.save.payload_changed else 'unchanged'}, "
                f"private={'changed' if self.save.private_changed else 'unchanged'}"
            )
        if self.errors:
            bits.append(f"errors: {'; '.join(self.errors)}")
        return " | ".join(bits)


def gather(config: Config, secrets: Secrets, store: Store, now: datetime) -> FetchResult:
    """Canvas assignments plus locally stored manual entries."""
    result = FetchResult()

    if secrets.canvas_token:
        source = CanvasSource(
            config.canvas_base_url,
            secrets.canvas_token,
            timezone=config.timezone,
            lookahead_days=config.lookahead_days,
        )
        result = _with_retries(lambda: source.fetch(now))
    else:
        log.warning("CANVAS_TOKEN not set; skipping Canvas and using manual entries only")

    result.deadlines.extend(store.manual_deadlines())
    return result


def _with_retries(call):
    """Retry a transient network failure before failing the whole tick.

    A failed tick surfaces as a GitHub Actions failure email, which is the
    intended alerting path — but it should mean "something is actually wrong",
    not "Canvas hiccuped for two seconds".
    """
    last: Exception | None = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - re-raised below
            last = exc
            if attempt == FETCH_ATTEMPTS:
                break
            wait = FETCH_BACKOFF_SECONDS * attempt
            log.warning("fetch attempt %d failed (%s); retrying in %ds", attempt, exc, wait)
            time.sleep(wait)
    raise last  # type: ignore[misc]


def run_tick(
    config: Config,
    secrets: Secrets,
    store: Store,
    now: datetime,
    *,
    dry_run: bool = False,
    skip_calendar: bool = False,
    skip_email: bool = False,
) -> TickReport:
    report = TickReport(now=now)

    fetched = gather(config, secrets, store, now)
    report.deadlines = fetched.deadlines
    store.private.submission_states = dict(fetched.submissions)
    for course_id, name in fetched.courses.items():
        store.private.course_settings.setdefault(course_id, {"enabled": True, "name": name})["name"] = name

    store.apply_manual_done()
    store.set_canvas_cache(fetched.deadlines)
    active = [d for d in fetched.deadlines if store.course_enabled(d.course_id)]

    # --- calendar --------------------------------------------------------
    if not skip_calendar and secrets.has_calendar:
        try:
            report.calendar = _sync_calendar(config, secrets, store, active, now, dry_run)
        except Exception as exc:  # noqa: BLE001
            # A calendar outage must not swallow the email reminders, which are
            # the part that actually gets someone to submit on time.
            log.error("calendar sync failed: %s", exc)
            report.errors.append(f"calendar: {exc}")
    elif not secrets.has_calendar:
        log.warning(
            "GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_CALENDAR_ID not set; skipping calendar sync"
        )

    # --- reminders -------------------------------------------------------
    pending = due_reminders(
        active,
        now,
        thresholds=config.thresholds,
        ledger=store.private.reminder_ledger,
        dismissed=store.private.dismissed,
        submissions=store.private.submission_states,
        skip_submitted=config.skip_submitted,
        course_enabled=store.course_enabled,
    )
    report.reminders = pending

    if pending and not skip_email:
        subject, text, html = render_digest(pending, now)
        mailer = build_mailer(secrets, store.private.recipient_email, dry_run=dry_run)
        sent = mailer.send(subject, text, html)
        report.reminder_sent = sent
        if sent:
            # Only after a confirmed send, so a failed SMTP call retries next tick.
            record(store.private.reminder_ledger, pending, now)

    # --- daily brief -----------------------------------------------------
    if not skip_email and should_send_brief(
        now, store.private.last_brief_date, config.brief_hour
    ):
        rows = brief_window(
            active,
            now,
            config.brief_lookahead_days,
            dismissed=store.private.dismissed,
            submissions=store.private.submission_states,
            skip_submitted=config.skip_submitted,
            course_enabled=store.course_enabled,
        )
        subject, text, html = render_brief(rows, now, config.brief_lookahead_days)
        mailer = build_mailer(secrets, store.private.recipient_email, dry_run=dry_run)
        if mailer.send(subject, text, html):
            report.brief_sent = True
            store.private.last_brief_date = now.date().isoformat()

    # --- heartbeat + persist ---------------------------------------------
    heartbeat = should_heartbeat(now, store.private.last_heartbeat_date, config.heartbeat_hour)
    if heartbeat:
        report.heartbeat = True
        store.private.last_heartbeat_date = now.date().isoformat()
        store.private.last_sync_at = now.isoformat()

    report.save = store.save(
        fetched.deadlines, now, force_payload=heartbeat, dry_run=dry_run
    )
    return report


def _sync_calendar(
    config: Config,
    secrets: Secrets,
    store: Store,
    deadlines: list[Deadline],
    now: datetime,
    dry_run: bool,
) -> ReconcilePlan:
    tokens = TokenProvider(ServiceAccount.parse(secrets.google_service_account_json))
    client = CalendarClient(tokens)
    calendar_id = client.verify_calendar(secrets.google_calendar_id)
    store.private.calendar_id = calendar_id

    return reconcile(
        client,
        calendar_id,
        deadlines,
        now=now,
        block_minutes=config.block_minutes,
        timezone_name=str(config.timezone),
        submissions=store.private.submission_states,
        dismissed=store.private.dismissed,
        event_ids=store.private.calendar_event_ids,
        lookahead_days=config.lookahead_days,
        dry_run=dry_run,
    )
