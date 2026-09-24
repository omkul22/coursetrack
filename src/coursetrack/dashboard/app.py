"""Local read-write dashboard.

Binds to 127.0.0.1 only. It serves decrypted state — real titles for private
entries included — so it must never be reachable from the network.
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import gitops
from ..config import Config, Secrets
from ..models import MANUAL, Deadline, manual_id
from ..store import Store

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

# The scheduled job refreshes the public timestamp on data changes and once a
# day at the heartbeat hour, so anything under ~26h old is healthy.
STALE_AFTER_HOURS = 26


class NewDeadline(BaseModel):
    title: str
    due_at: str
    course: str = "Other"
    url: str | None = None
    private: bool = False


class CourseToggle(BaseModel):
    enabled: bool


def create_app(config: Config, secrets: Secrets) -> FastAPI:
    app = FastAPI(title="coursetrack", docs_url=None, redoc_url=None)

    def fresh_store() -> Store:
        """Reload per request; the scheduled job may have rewritten state."""
        return Store(config).load()

    def persist(store: Store, message: str) -> str:
        store.save(_all_deadlines(store, config), datetime.now(config.timezone))
        if not gitops.is_repo(config.root):
            return "saved locally (not a git repo)"
        ok, detail = gitops.commit_and_push(config.root, message)
        return detail if ok else f"WARNING: {detail}"

    # ---- read ------------------------------------------------------------

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        store = fresh_store()
        now = datetime.now(config.timezone)
        deadlines = _all_deadlines(store, config)

        rows = []
        for d in sorted(deadlines, key=lambda d: d.due_at):
            submission = store.private.submission_states.get(d.id, "")
            rows.append(
                {
                    "id": d.id,
                    "source": d.source,
                    "course": d.course,
                    "course_id": d.course_id,
                    "title": d.title,  # real title: this page is local-only
                    "due_at": d.due_at.isoformat(),
                    "url": d.url,
                    "private": d.private,
                    "submission": submission,
                    "dismissed": d.id in store.private.dismissed,
                    "course_enabled": store.course_enabled(d.course_id),
                    "reminders": sorted(
                        store.private.reminder_ledger.get(d.id, {}), key=int, reverse=True
                    ),
                }
            )

        return {
            "now": now.isoformat(),
            "deadlines": rows,
            "courses": [
                {"id": cid, "name": meta.get("name", cid), "enabled": meta.get("enabled", True)}
                for cid, meta in sorted(
                    store.private.course_settings.items(),
                    key=lambda kv: kv[1].get("name", kv[0]),
                )
            ],
            "thresholds": list(config.threshold_hours),
            "health": _health(config, now, store),
            "recipient": store.private.recipient_email,
        }

    # ---- write -----------------------------------------------------------

    @app.post("/api/deadlines")
    def add(payload: NewDeadline) -> dict[str, Any]:
        store = fresh_store()
        try:
            due = datetime.fromisoformat(payload.due_at)
        except ValueError:
            raise HTTPException(400, "due_at must be ISO 8601, e.g. 2026-09-25T23:59")
        if due.tzinfo is None:
            due = due.replace(tzinfo=config.timezone)

        deadline = Deadline(
            id=manual_id(),
            source=MANUAL,
            course=payload.course or "Other",
            course_id="",
            title=payload.title.strip(),
            due_at=due.astimezone(config.timezone),
            url=payload.url or None,
            private=payload.private,
        )
        if not deadline.title:
            raise HTTPException(400, "title is required")

        store.add_manual(deadline)
        # Never put a private title into a commit message — it lands in the
        # public repo's history just as surely as a file would.
        label = "private deadline" if deadline.private else deadline.title
        return {"id": deadline.id, "git": persist(store, f"add: {label}")}

    @app.delete("/api/deadlines/{deadline_id}")
    def remove(deadline_id: str) -> dict[str, Any]:
        store = fresh_store()
        if not store.remove_manual(deadline_id):
            raise HTTPException(404, "not a manual deadline")
        return {"git": persist(store, "remove manual deadline")}

    @app.post("/api/deadlines/{deadline_id}/dismiss")
    def dismiss(deadline_id: str, undo: bool = False) -> dict[str, Any]:
        store = fresh_store()
        if undo:
            store.private.dismissed = [d for d in store.private.dismissed if d != deadline_id]
        elif deadline_id not in store.private.dismissed:
            store.private.dismissed.append(deadline_id)
        return {"git": persist(store, "update dismissed deadlines")}

    @app.post("/api/courses/{course_id}/toggle")
    def toggle(course_id: str, payload: CourseToggle) -> dict[str, Any]:
        store = fresh_store()
        store.set_course_enabled(course_id, payload.enabled)
        return {"git": persist(store, f"course {course_id}: enabled={payload.enabled}")}

    @app.post("/api/sync")
    def sync() -> dict[str, Any]:
        from ..engine import run_tick

        store = fresh_store()
        gitops.pull_rebase(config.root)
        report = run_tick(
            config,
            secrets,
            store,
            datetime.now(config.timezone),
            skip_email=True,  # a manual refresh should never fire reminders
        )
        detail = "saved locally"
        if gitops.is_repo(config.root):
            ok, detail = gitops.commit_and_push(config.root, "sync: refresh deadlines")
            if not ok:
                detail = f"WARNING: {detail}"
        return {"summary": report.summary(), "errors": report.errors, "git": detail}

    # ---- static ----------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


def _all_deadlines(store: Store, config: Config) -> list[Deadline]:
    """Manual entries plus whatever the last sync published.

    Canvas rows come from the public file rather than a fresh fetch, so opening
    the dashboard is instant and works offline. Shared with the CLI so a write
    from either side republishes the same complete list.
    """
    return store.all_deadlines()


def _health(config: Config, now: datetime, store: Store) -> dict[str, Any]:
    """Freshness of the last successful sync.

    Read from the encrypted state rather than a file timestamp: the payload is
    only rewritten when something changes or at the daily heartbeat, so its
    mtime says nothing useful about whether the job is alive.
    """
    stamp = store.private.last_sync_at
    if not stamp:
        return {"ok": False, "detail": "no sync yet"}
    try:
        synced = datetime.fromisoformat(stamp)
    except ValueError:
        return {"ok": False, "detail": "state timestamp unreadable"}

    age = (now - synced).total_seconds() / 3600
    return {
        "ok": age < STALE_AFTER_HOURS,
        "age_hours": round(age, 1),
        "generated_at": synced.isoformat(),
        "detail": f"last sync {round(age, 1)}h ago",
    }


def serve(config: Config, secrets: Secrets, port: int = 8787, open_browser: bool = True) -> None:
    import uvicorn

    if gitops.is_repo(config.root):
        ok, detail = gitops.pull_rebase(config.root)
        log.info("git pull: %s", detail if detail else "up to date")
        if not ok:
            log.warning("could not pull latest state: %s", detail)

    url = f"http://127.0.0.1:{port}"
    print(f"\n  coursetrack dashboard -> {url}\n  ctrl-c to stop\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        create_app(config, secrets),
        host="127.0.0.1",  # never 0.0.0.0: this serves decrypted private data
        port=port,
        log_level="warning",
    )
