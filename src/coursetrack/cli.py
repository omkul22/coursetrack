"""Command line entrypoint. `python -m coursetrack <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import load_config, load_secrets, repo_root
from .crypto import ENV_VAR, KeyMissingError, generate_key, keychain_get, keychain_set
from .models import MANUAL, Deadline, manual_id
from .notify.templates import absolute, humanize_delta
from .sources.base import is_done
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return args.handler(args)
    except KeyMissingError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - top-level CLI boundary
        logging.getLogger("coursetrack").error("%s", exc)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coursetrack", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--root", type=Path, default=None, help="repo root (default: auto-detect)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-key", help="generate the state encryption key")
    p.add_argument(
        "--show",
        action="store_true",
        help="print the key to stdout (otherwise it only goes to the Keychain)",
    )
    p.set_defaults(handler=cmd_init_key)

    p = sub.add_parser(
        "google-check",
        help="show the service account email and verify calendar access",
    )
    p.set_defaults(handler=cmd_google_check)

    p = sub.add_parser("set-email", help="set the reminder recipient")
    p.add_argument("address")
    p.set_defaults(handler=cmd_set_email)

    p = sub.add_parser("tick", help="the full scheduled run")
    _add_run_flags(p)
    p.set_defaults(handler=cmd_tick)

    p = sub.add_parser("sync", help="refresh data and calendar, send no email")
    _add_run_flags(p)
    p.set_defaults(handler=cmd_sync)

    p = sub.add_parser("list", help="show tracked deadlines")
    p.add_argument("--all", action="store_true", help="include past-due")
    p.set_defaults(handler=cmd_list)

    p = sub.add_parser("add", help="add a manual deadline")
    p.add_argument("title")
    p.add_argument("due", help="ISO local time, e.g. 2026-09-25T23:59")
    p.add_argument("--course", default="Other")
    p.add_argument(
        "--course-id",
        default="",
        help="attach to an existing course id so it inherits that course's on/off toggle",
    )
    p.add_argument("--url", default=None)
    p.add_argument("--private", action="store_true", help="redact from the public file")
    p.set_defaults(handler=cmd_add)

    p = sub.add_parser("remove", help="delete a manual deadline by id")
    p.add_argument("deadline_id")
    p.set_defaults(handler=cmd_remove)

    p = sub.add_parser("courses", help="list courses, or enable/disable them")
    p.add_argument("--enable", nargs="+", metavar="ID", default=[])
    p.add_argument("--disable", nargs="+", metavar="ID", default=[])
    p.add_argument(
        "--disable-empty",
        action="store_true",
        help="disable every course with no tracked deadlines",
    )
    p.add_argument("--add", metavar="NAME", help="create a course Canvas does not know about")
    p.add_argument("--remove", metavar="ID", help="delete a custom course")
    p.set_defaults(handler=cmd_courses)

    p = sub.add_parser("test-email", help="send one test message")
    p.set_defaults(handler=cmd_test_email)

    p = sub.add_parser("status", help="show state health")
    p.set_defaults(handler=cmd_status)

    p = sub.add_parser("dash", help="run the local dashboard")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(handler=cmd_dash)

    return parser


def _add_run_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="compute everything, change nothing")
    p.add_argument("--now", default=None, help="override the clock, e.g. 2026-09-24T12:00")
    p.add_argument("--skip-calendar", action="store_true")
    p.add_argument("--skip-email", action="store_true")


# --- helpers ---------------------------------------------------------------


def _context(args):
    root = args.root or repo_root(Path.cwd())
    config = load_config(root)
    secrets = load_secrets(root / ".env")
    return config, secrets


def _resolve_now(args, config) -> datetime:
    """`--now` is what makes threshold behaviour testable without waiting."""
    if not getattr(args, "now", None):
        return datetime.now(config.timezone)
    parsed = datetime.fromisoformat(args.now)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.timezone)
    return parsed.astimezone(config.timezone)


# --- commands --------------------------------------------------------------


def cmd_init_key(args) -> int:
    if keychain_get() and not args.show:
        print(
            "\nA key already exists in your Keychain. Generating a new one would make "
            "the existing data/private.enc permanently unreadable.\n\n"
            "  To see the existing key:  coursetrack init-key --show\n"
            "  To replace it anyway:     delete the 'coursetrack' entry in Keychain Access first\n"
        )
        return 1

    key = generate_key()
    try:
        keychain_set(key)
        stored = True
    except Exception:  # noqa: BLE001 - non-macOS or Keychain unavailable
        stored = False

    if stored:
        print("\nGenerated a new state key and stored it in your login Keychain.")
        print("It was deliberately NOT printed, so it stays out of your terminal history.\n")
        print("To read it back (for the GitHub secret, or a backup):\n")
        print("  security find-generic-password -s coursetrack -a state-key -w\n")
    else:
        # Nothing stored it, so printing is the only way to not lose it.
        print("\nGenerated a new state key. Your Keychain was unavailable, so save this now:\n")
        print(f"  {key}\n")

    if args.show:
        print(f"  {key}\n")

    print("Back this up somewhere safe. Losing it makes data/private.enc unreadable")
    print("(calendar links rebuild themselves; the reminder ledger does not).\n")
    print(f"For a local shell you can also export ${ENV_VAR}.\n")
    return 0


def cmd_google_check(args) -> int:
    """Walk the calendar setup one stage at a time and say exactly what is wrong.

    Ordered so the first failure is the one to act on: parse the key, print
    the address the calendar must be shared with, mint a token, then read the
    calendar back.
    """
    from .google.calendar import CalendarClient, CalendarError
    from .google.service_account import ServiceAccount, ServiceAccountError, TokenProvider

    _, secrets = _context(args)

    try:
        account = ServiceAccount.parse(secrets.google_service_account_json)
    except ServiceAccountError as exc:
        print(f"\n  key      FAILED\n           {exc}\n")
        return 1
    print(f"\n  key      OK    {account.client_email}")

    if not secrets.google_calendar_id:
        print(
            "\n  calendar NOT SET\n"
            "           Create a calendar in Google Calendar, then:\n"
            "             Settings and sharing -> Share with specific people -> Add people\n"
            f"             add  {account.client_email}\n"
            "             permission: 'Make changes to events'\n"
            "           Then copy the Calendar ID from 'Integrate calendar'\n"
            "           into GOOGLE_CALENDAR_ID in .env\n"
        )
        return 1

    tokens = TokenProvider(account)
    try:
        tokens.token()
    except ServiceAccountError as exc:
        print(f"  token    FAILED\n           {exc}\n")
        return 1
    print("  token    OK    minted an access token")

    try:
        CalendarClient(tokens).verify_calendar(secrets.google_calendar_id)
    except CalendarError as exc:
        print(f"  calendar FAILED\n           {exc}\n")
        return 1
    print(f"  calendar OK    {secrets.google_calendar_id}\n")
    print("  Calendar access is working.\n")
    return 0


def cmd_set_email(args) -> int:
    config, _ = _context(args)
    store = Store(config).load()
    store.private.recipient_email = args.address
    store.save(store.all_deadlines(), datetime.now(config.timezone))
    print(f"Reminders will go to {args.address} (stored encrypted).")
    return 0


def cmd_tick(args, skip_email: bool | None = None) -> int:
    from .engine import run_tick

    config, secrets = _context(args)
    store = Store(config).load()
    now = _resolve_now(args, config)

    report = run_tick(
        config,
        secrets,
        store,
        now,
        dry_run=args.dry_run,
        skip_calendar=args.skip_calendar,
        skip_email=args.skip_email if skip_email is None else skip_email,
    )
    print(report.summary())
    return 1 if report.errors else 0


def cmd_sync(args) -> int:
    return cmd_tick(args, skip_email=True)


def cmd_list(args) -> int:
    config, _ = _context(args)
    now = datetime.now(config.timezone)
    store = Store(config).load()

    rows = sorted(store.all_deadlines(), key=lambda d: d.due_at)
    if not rows:
        print("No data yet. Run `coursetrack sync` first.")
        return 0
    if not args.all:
        rows = [d for d in rows if d.due_at > now]
    if not rows:
        print("Nothing upcoming.")
        return 0

    for deadline in rows:
        state = store.private.submission_states.get(deadline.id, "")
        done = "done" if is_done(state) else "    "
        print(
            f"  {done} {humanize_delta(deadline.due_at, now):>13}  "
            f"{absolute(deadline.due_at):<22} {deadline.title[:42]:<42} {deadline.course[:26]}"
        )
    print(f"\n{len(rows)} deadline(s). Last sync {store.private.last_sync_at or 'unknown'}.")
    return 0


def cmd_add(args) -> int:
    config, _ = _context(args)
    store = Store(config).load()

    due = datetime.fromisoformat(args.due)
    if due.tzinfo is None:
        due = due.replace(tzinfo=config.timezone)

    deadline = Deadline(
        id=manual_id(),
        source=MANUAL,
        course=args.course,
        course_id=args.course_id,
        title=args.title,
        due_at=due,
        url=args.url,
        private=args.private,
    )
    store.add_manual(deadline)
    store.save(store.all_deadlines(), datetime.now(config.timezone))
    flag = " (private)" if args.private else ""
    print(f"Added {deadline.title!r} due {absolute(due)}{flag}")
    print("Run `coursetrack sync` to create the calendar event.")
    return 0


def cmd_remove(args) -> int:
    config, _ = _context(args)
    store = Store(config).load()
    if not store.remove_manual(args.deadline_id):
        print(f"No manual deadline with id {args.deadline_id}")
        return 1
    store.save(store.all_deadlines(), datetime.now(config.timezone))
    print("Removed. Run `coursetrack sync` to delete the calendar event.")
    return 0


def cmd_courses(args) -> int:
    """List courses with their deadline counts, and toggle which are tracked.

    A disabled course is filtered out before anything is written or sent, so
    disabling is also the way to keep a course name out of the published file.
    """
    config, _ = _context(args)
    store = Store(config).load()

    counts: dict[str, int] = {}
    for deadline in store.all_deadlines():
        counts[deadline.course_id] = counts.get(deadline.course_id, 0) + 1

    if args.add:
        course_id = store.add_course(args.add)
        print(f"  course id: {course_id}  ({args.add})")
    if args.remove and not store.remove_course(args.remove):
        print(f"  {args.remove} is not a custom course; only custom courses can be removed")
        return 1

    if args.disable_empty:
        for course_id in store.private.course_settings:
            if not counts.get(course_id):
                store.set_course_enabled(course_id, False)
    for course_id in args.disable:
        store.set_course_enabled(course_id, False)
    for course_id in args.enable:
        store.set_course_enabled(course_id, True)

    changed = bool(args.enable or args.disable or args.disable_empty or args.add or args.remove)
    if changed:
        store.save(store.all_deadlines(), datetime.now(config.timezone))

    settings = store.private.course_settings
    if not settings:
        print("No courses known yet. Run `coursetrack sync` first.")
        return 0

    for course_id, meta in sorted(settings.items(), key=lambda kv: kv[1].get("name", kv[0])):
        mark = "on " if meta.get("enabled", True) else "off"
        n = counts.get(course_id, 0)
        print(f"  [{mark}] {course_id:>7}  {meta.get('name', course_id)[:54]:<54} {n:>2} deadline(s)")

    if changed:
        active = sum(1 for m in settings.values() if m.get("enabled", True))
        print(f"\nSaved. {active} of {len(settings)} course(s) tracked.")
    return 0


def cmd_test_email(args) -> int:
    from .notify.email import build_mailer

    config, secrets = _context(args)
    store = Store(config).load()
    now = datetime.now(config.timezone)
    mailer = build_mailer(secrets, store.private.recipient_email)
    mailer.send(
        "CourseTrack test",
        f"If you are reading this, SMTP works.\n\nSent {absolute(now)}\n",
        f"<p>If you are reading this, SMTP works.</p><p>Sent {absolute(now)}</p>",
    )
    print(f"Sent a test message to {store.private.recipient_email}")
    return 0


def cmd_status(args) -> int:
    import json

    config, secrets = _context(args)
    store = Store(config).load()
    now = datetime.now(config.timezone)

    print(f"root            {config.root}")
    print(f"timezone        {config.timezone}")
    print(f"canvas          {config.canvas_base_url}")
    print(f"recipient       {store.private.recipient_email or '(not set)'}")
    print(f"calendar id     {store.private.calendar_id or '(not created yet)'}")
    print(f"manual entries  {len(store.private.manual_entries)}")
    print(f"ledger entries  {len(store.private.reminder_ledger)}")
    print(f"last brief      {store.private.last_brief_date or '(never)'}")
    print(f"last heartbeat  {store.private.last_heartbeat_date or '(never)'}")

    for name in ("canvas_token", "google_service_account_json", "google_calendar_id", "gmail_app_password"):
        print(f"{name:<15} {'set' if getattr(secrets, name) else 'MISSING'}")

    print(f"deadlines       {len(store.all_deadlines())} cached")
    print(f"payload         {'present' if config.payload_path.is_file() else 'not built yet'}")
    return 0


def cmd_dash(args) -> int:
    from .dashboard.app import serve

    config, secrets = _context(args)
    serve(config, secrets, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
