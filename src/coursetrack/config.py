"""Non-secret config from config.toml, and secrets from the environment.

The split is deliberate and load-bearing: anything read from `config.toml` is
safe to commit to a public repo, and anything read from `secrets()` is not.
Keeping them in separate functions makes it hard to accidentally write a
secret into a committed file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_CONFIG_NAME = "config.toml"


def repo_root(start: Path | None = None) -> Path:
    """Walk up from `start` until a directory containing config.toml appears."""
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / DEFAULT_CONFIG_NAME).is_file():
            return candidate
    raise FileNotFoundError(
        f"Could not locate {DEFAULT_CONFIG_NAME} in any parent of {here}"
    )


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    timezone: ZoneInfo
    canvas_base_url: str
    lookahead_days: int
    threshold_hours: tuple[int, ...]
    brief_hour: int
    brief_lookahead_days: int
    skip_submitted: bool
    calendar_name: str
    block_minutes: int
    heartbeat_hour: int

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def docs_dir(self) -> Path:
        """Served by GitHub Pages."""
        return self.root / "docs"

    @property
    def payload_path(self) -> Path:
        """Encrypted payload the hosted dashboard downloads."""
        return self.docs_dir / "data.enc"

    @property
    def private_path(self) -> Path:
        return self.data_dir / "private.enc"

    @property
    def thresholds(self) -> tuple[timedelta, ...]:
        """Longest-first, so a digest lists the earliest warning first."""
        return tuple(
            timedelta(hours=h) for h in sorted(self.threshold_hours, reverse=True)
        )


def load_config(root: Path | None = None) -> Config:
    root = root or repo_root()
    with (root / DEFAULT_CONFIG_NAME).open("rb") as handle:
        raw = tomllib.load(handle)

    general = raw.get("general", {})
    canvas = raw.get("canvas", {})
    reminders = raw.get("reminders", {})
    calendar = raw.get("calendar", {})
    maintenance = raw.get("maintenance", {})

    return Config(
        root=root,
        timezone=ZoneInfo(general.get("timezone", "America/New_York")),
        canvas_base_url=canvas.get("base_url", "").rstrip("/"),
        lookahead_days=int(canvas.get("lookahead_days", 90)),
        threshold_hours=tuple(reminders.get("thresholds_hours", [72, 12])),
        brief_hour=int(reminders.get("brief_hour", 7)),
        brief_lookahead_days=int(reminders.get("brief_lookahead_days", 7)),
        skip_submitted=bool(reminders.get("skip_submitted", True)),
        calendar_name=calendar.get("calendar_name", "Coursework"),
        block_minutes=int(calendar.get("block_minutes", 30)),
        heartbeat_hour=int(maintenance.get("heartbeat_hour", 3)),
    )


@dataclass(frozen=True, slots=True)
class Secrets:
    canvas_token: str = ""
    # Base64 (or raw JSON) of the service account key file.
    google_service_account_json: str = ""
    # The one calendar shared with that service account.
    google_calendar_id: str = ""
    gmail_address: str = ""
    gmail_app_password: str = ""

    @property
    def has_calendar(self) -> bool:
        return bool(self.google_service_account_json and self.google_calendar_id)

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env_names = ", ".join(_ENV_FOR[n] for n in missing)
            raise RuntimeError(f"Missing required secret(s): {env_names}")


_ENV_FOR = {
    "canvas_token": "CANVAS_TOKEN",
    "google_service_account_json": "GOOGLE_SERVICE_ACCOUNT_JSON",
    "google_calendar_id": "GOOGLE_CALENDAR_ID",
    "gmail_address": "GMAIL_ADDRESS",
    "gmail_app_password": "GMAIL_APP_PASSWORD",
}


def load_secrets(dotenv: Path | None = None) -> Secrets:
    """Secrets from the environment, optionally seeded from a gitignored .env.

    `.env` never overrides a real environment variable, so GitHub Actions
    secrets always win over a stale local file.
    """
    if dotenv and dotenv.is_file():
        _seed_from_dotenv(dotenv)
    return Secrets(**{field_: os.environ.get(env, "") for field_, env in _ENV_FOR.items()})


def write_dotenv_value(path: Path, name: str, value: str) -> bool:
    """Set NAME=value in a .env file, in place, without printing the value.

    Replaces the first matching assignment and appends if absent. Keeps the
    file at mode 600 so a freshly written secret is never world-readable.
    Returns True if an existing line was replaced.
    """
    lines = path.read_text().splitlines() if path.is_file() else []
    replaced = False
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == name:
            lines[index] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")

    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return replaced


def _seed_from_dotenv(path: Path) -> None:
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(name, value)
