"""Google Calendar reconciliation against a dedicated "Coursework" calendar.

Idempotency has two layers:

  1. `calendar_event_ids` in private state — fast, avoids a lookup.
  2. `extendedProperties.private.coursetrack_id` stamped on every event — the
     authoritative link.

The second layer is what makes a lost or unreadable `private.enc` recoverable:
the events themselves still say which deadline they belong to, so a rebuild
re-adopts them instead of creating a duplicate of everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

import httpx

from ..models import Deadline
from ..sources.base import is_done

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3"

MANAGED_FLAG = "coursetrack_managed"
ID_PROP = "coursetrack_id"
DONE_PREFIX = "✅ "


class CalendarError(RuntimeError):
    pass


@dataclass
class ReconcilePlan:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.created or self.updated or self.deleted)

    def summary(self) -> str:
        return (
            f"{len(self.created)} created, "
            f"{len(self.updated)} updated, "
            f"{len(self.deleted)} deleted"
        )


class CalendarClient:
    def __init__(self, token_provider, *, client: httpx.Client | None = None, timeout: float = 30.0):
        self._tokens = token_provider
        self._client = client
        self._timeout = timeout

    def _request(self, method: str, path: str, **kwargs) -> Any:
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.request(
                method,
                f"{API}{path}",
                headers={"Authorization": f"Bearer {self._tokens.token()}"},
                **kwargs,
            )
        finally:
            if self._client is None:
                client.close()

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise CalendarError(
                f"Calendar API {method} {path} failed ({response.status_code}): "
                f"{response.text[:300]}"
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    # ---- calendar ---------------------------------------------------------

    def verify_calendar(self, calendar_id: str) -> str:
        """Confirm the configured calendar exists and we can reach it.

        The service account cannot create a calendar in someone else's account,
        so the calendar is made by hand once and shared with it. That is the
        feature, not the limitation: access is scoped to this single calendar
        instead of every calendar in the account.
        """
        if not calendar_id:
            raise CalendarError(
                "GOOGLE_CALENDAR_ID is not set. Create a calendar in Google Calendar, "
                "share it with the service account, and put its ID in .env."
            )

        try:
            calendar = self._request("GET", f"/calendars/{calendar_id}")
        except CalendarError as exc:
            # 404 comes back as None; a calendar that exists but is not shared
            # returns 403, which would otherwise surface as a raw API error.
            if "403" not in str(exc):
                raise
            calendar = None

        if calendar is None:
            raise CalendarError(
                f"Calendar {calendar_id!r} is not reachable. Either the ID is wrong, or it "
                "has not been shared with the service account. In Google Calendar open "
                "Settings and sharing -> Share with specific people -> add the service "
                "account email with 'Make changes to events'."
            )
        log.info("using calendar %r (%s)", calendar.get("summary", "?"), calendar_id)
        return calendar_id

    # ---- events -----------------------------------------------------------

    def managed_events(self, calendar_id: str, time_min: datetime, time_max: datetime) -> dict[str, dict]:
        """Every event this tool owns, keyed by deadline id — in one request.

        Querying per deadline would be N round trips; filtering on the managed
        flag gets the whole picture at once and also reveals orphans whose
        deadline has disappeared from the source.
        """
        events: dict[str, dict] = {}
        page_token = None
        while True:
            params = {
                "privateExtendedProperty": f"{MANAGED_FLAG}=1",
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "maxResults": 2500,
                "singleEvents": "true",
                "showDeleted": "false",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._request("GET", f"/calendars/{calendar_id}/events", params=params) or {}
            for event in payload.get("items", []):
                key = (event.get("extendedProperties", {}).get("private", {}) or {}).get(ID_PROP)
                if key:
                    events[key] = event
            page_token = payload.get("nextPageToken")
            if not page_token:
                return events

    def insert(self, calendar_id: str, body: dict) -> dict:
        return self._request("POST", f"/calendars/{calendar_id}/events", json=body)

    def patch(self, calendar_id: str, event_id: str, body: dict) -> dict:
        return self._request("PATCH", f"/calendars/{calendar_id}/events/{event_id}", json=body)

    def delete(self, calendar_id: str, event_id: str) -> None:
        self._request("DELETE", f"/calendars/{calendar_id}/events/{event_id}")


def event_body(
    deadline: Deadline,
    *,
    block_minutes: int,
    timezone_name: str,
    submitted: bool = False,
) -> dict[str, Any]:
    """A 30-minute block ending exactly at the due time.

    Private deadlines use their real title here on purpose — this is the
    user's own calendar, and showing real titles is the payoff for encrypting
    them rather than omitting them.
    """
    start = deadline.due_at - timedelta(minutes=block_minutes)
    summary = f"DUE: {deadline.title} — {deadline.course}"
    if submitted:
        summary = DONE_PREFIX + summary

    description_lines = []
    if deadline.url:
        description_lines.append(deadline.url)
    description_lines.append(f"Source: {deadline.source}")
    description_lines.append("Managed by coursetrack — edits here are overwritten.")

    return {
        "summary": summary,
        "description": "\n".join(description_lines),
        "start": {"dateTime": start.isoformat(), "timeZone": timezone_name},
        "end": {"dateTime": deadline.due_at.isoformat(), "timeZone": timezone_name},
        "extendedProperties": {
            "private": {MANAGED_FLAG: "1", ID_PROP: deadline.id},
        },
    }


def needs_update(existing: dict, desired: dict) -> bool:
    """Compare only the fields this tool owns.

    Google echoes back a large event resource with server-set fields; diffing
    the whole thing would report a change every run.
    """
    if existing.get("summary") != desired["summary"]:
        return True
    if existing.get("description") != desired["description"]:
        return True
    for end in ("start", "end"):
        have = (existing.get(end) or {}).get("dateTime")
        want = desired[end]["dateTime"]
        if have is None or not _same_instant(have, want):
            return True
    return False


def _same_instant(a: str, b: str) -> bool:
    """Google may normalise the offset representation; compare as instants."""
    try:
        return datetime.fromisoformat(a) == datetime.fromisoformat(b)
    except ValueError:
        return a == b


def reconcile(
    client: CalendarClient,
    calendar_id: str,
    deadlines: Sequence[Deadline],
    *,
    now: datetime,
    block_minutes: int,
    timezone_name: str,
    submissions: Mapping[str, str] | None = None,
    dismissed: Iterable[str] = (),
    event_ids: dict[str, str] | None = None,
    lookahead_days: int = 90,
    dry_run: bool = False,
) -> ReconcilePlan:
    """Make the calendar match the deadline list, and prune what no longer exists."""
    submissions = submissions or {}
    dismissed = set(dismissed)
    event_ids = event_ids if event_ids is not None else {}
    plan = ReconcilePlan()

    existing = client.managed_events(
        calendar_id,
        now - timedelta(days=30),
        now + timedelta(days=lookahead_days + 1),
    )

    wanted = {d.id: d for d in deadlines if d.id not in dismissed}

    for deadline_id, deadline in wanted.items():
        desired = event_body(
            deadline,
            block_minutes=block_minutes,
            timezone_name=timezone_name,
            submitted=is_done(submissions.get(deadline_id, "")),
        )
        found = existing.get(deadline_id)

        if found is None:
            if deadline.due_at < now:
                # Deadlines stay visible for a grace period after they pass so
                # the dashboard can show them as overdue. Creating a calendar
                # event now would be churn: it drops out of that window within
                # days, becomes an orphan, and gets deleted again. Existing
                # events for past deadlines are left alone below.
                continue
            plan.created.append(deadline_id)
            if not dry_run:
                created = client.insert(calendar_id, desired)
                event_ids[deadline_id] = created["id"]
        elif needs_update(found, desired):
            plan.updated.append(deadline_id)
            event_ids[deadline_id] = found["id"]
            if not dry_run:
                client.patch(calendar_id, found["id"], desired)
        else:
            event_ids[deadline_id] = found["id"]

    for orphan_id, event in existing.items():
        if orphan_id in wanted:
            continue
        plan.deleted.append(orphan_id)
        event_ids.pop(orphan_id, None)
        if not dry_run:
            client.delete(calendar_id, event["id"])

    log.info("calendar: %s", plan.summary())
    return plan
