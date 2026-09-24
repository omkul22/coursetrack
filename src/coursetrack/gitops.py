"""Thin git wrapper so dashboard edits reach the repo the scheduled job reads.

The repo is the database, so "save" means "commit and push". Every helper
returns (ok, message) instead of raising: a push failing because the network
is down should surface in the UI, not take the dashboard with it.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

TIMEOUT = 60


def _run(root: Path, *args: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def is_repo(root: Path) -> bool:
    ok, _ = _run(root, "rev-parse", "--git-dir")
    return ok


def has_remote(root: Path) -> bool:
    ok, out = _run(root, "remote")
    return ok and bool(out.strip())


def is_dirty(root: Path, *paths: str) -> bool:
    ok, out = _run(root, "status", "--porcelain", "--", *paths)
    return ok and bool(out.strip())


def pull_rebase(root: Path) -> tuple[bool, str]:
    """Catch up with whatever the scheduled job committed since last time."""
    if not has_remote(root):
        return True, "no remote configured"
    return _run(root, "pull", "--rebase", "--autostash")


def commit_and_push(root: Path, message: str, *paths: str) -> tuple[bool, str]:
    paths = paths or ("data",)

    ok, out = _run(root, "add", "--", *paths)
    if not ok:
        return False, f"git add failed: {out}"

    if not is_dirty(root, *paths):
        return True, "nothing to commit"

    ok, out = _run(root, "commit", "-m", message, "--", *paths)
    if not ok:
        return False, f"git commit failed: {out}"

    if not has_remote(root):
        return True, "committed locally (no remote configured)"

    # Rebase first so a push never fails just because the hourly job landed
    # a commit thirty seconds ago.
    pull_rebase(root)
    ok, out = _run(root, "push")
    if not ok:
        return False, f"committed, but push failed: {out}"
    return True, "committed and pushed"
