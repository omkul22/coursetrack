"""Email rendering. Plain text is the real payload; HTML is a nicety.

Every message is built with both parts so it stays readable in a terminal
client, in Gmail's preview line, and on a watch.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Sequence

from ..models import Deadline
from ..reminders import PendingReminder


def humanize_delta(due_at: datetime, now: datetime) -> str:
    seconds = (due_at - now).total_seconds()
    if seconds < 0:
        return "overdue"
    hours = seconds / 3600
    if hours < 1:
        return f"in {int(seconds // 60)} min"
    if hours < 36:
        return f"in {round(hours)} hours"
    return f"in {round(hours / 24)} days"


def absolute(due_at: datetime) -> str:
    # %-I / %-d are POSIX; fine on macOS and the Linux runners.
    return due_at.strftime("%a %b %-d, %-I:%M %p")


def threshold_label(hours: int) -> str:
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''}"


# --- digest ----------------------------------------------------------------


def render_digest(
    pending: Sequence[PendingReminder], now: datetime
) -> tuple[str, str, str]:
    if len(pending) == 1:
        item = pending[0]
        subject = f"Due in {threshold_label(item.label_hours)}: {item.deadline.title}"
    else:
        subject = f"{len(pending)} deadlines approaching"

    text_rows, html_rows = [], []
    for item in pending:
        d = item.deadline
        text_rows.append(
            f"  • {d.title}\n"
            f"      {d.course}\n"
            f"      due {absolute(d.due_at)} ({humanize_delta(d.due_at, now)})"
            + (f"\n      {d.url}" if d.url else "")
        )
        html_rows.append(_row_html(d, now))

    text = (
        f"{subject}\n\n"
        + "\n\n".join(text_rows)
        + f"\n\n—\nchecked {absolute(now)}\n"
    )
    return subject, text, _wrap_html(subject, html_rows, now)


# --- daily brief -----------------------------------------------------------


def render_brief(
    deadlines: Sequence[Deadline], now: datetime, days: int
) -> tuple[str, str, str]:
    if not deadlines:
        subject = "Nothing due this week"
        text = f"{subject}\n\nNo deadlines in the next {days} days.\n"
        return subject, text, _wrap_html(subject, ["<p>Nothing on the board. Enjoy it.</p>"], now)

    subject = f"This week: {len(deadlines)} deadline{'s' if len(deadlines) != 1 else ''}"
    text_rows = [
        f"  • {d.title}\n"
        f"      {d.course}\n"
        f"      due {absolute(d.due_at)} ({humanize_delta(d.due_at, now)})"
        for d in deadlines
    ]
    text = (
        f"{subject}\n\nDue in the next {days} days:\n\n"
        + "\n\n".join(text_rows)
        + f"\n\n—\n{absolute(now)}\n"
    )
    html_rows = [_row_html(d, now) for d in deadlines]
    return subject, text, _wrap_html(subject, html_rows, now)


# --- html ------------------------------------------------------------------


def _row_html(deadline: Deadline, now: datetime) -> str:
    urgency = (deadline.due_at - now).total_seconds() / 3600
    accent = "#d9534f" if urgency <= 12 else "#e0a800" if urgency <= 72 else "#6c8cff"
    title = html.escape(deadline.title)
    if deadline.url:
        title = f'<a href="{html.escape(deadline.url)}" style="color:inherit">{title}</a>'
    return f"""
    <tr><td style="padding:12px 0;border-bottom:1px solid #e6e8eb">
      <div style="font-size:16px;font-weight:600;color:#14161a">{title}</div>
      <div style="font-size:13px;color:#61656c;margin-top:2px">{html.escape(deadline.course)}</div>
      <div style="font-size:13px;margin-top:6px">
        <span style="color:{accent};font-weight:600">{humanize_delta(deadline.due_at, now)}</span>
        <span style="color:#61656c"> &middot; {absolute(deadline.due_at)}</span>
      </div>
    </td></tr>"""


def _wrap_html(heading: str, rows: Sequence[str], now: datetime) -> str:
    return f"""<!doctype html><html><body style="margin:0;padding:24px;
background:#f5f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<table role="presentation" width="100%" style="max-width:520px;margin:0 auto;
background:#fff;border-radius:12px;padding:24px;border-collapse:collapse">
<tr><td style="padding-bottom:8px">
  <div style="font-size:18px;font-weight:700;color:#14161a">{html.escape(heading)}</div>
</td></tr>
{''.join(rows)}
<tr><td style="padding-top:16px;font-size:12px;color:#8b8f96">
  coursetrack &middot; {absolute(now)}
</td></tr>
</table></body></html>"""
