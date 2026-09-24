from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coursetrack.config import load_config
from coursetrack.crypto import generate_key
from coursetrack.models import Deadline, canvas_id, manual_id
from coursetrack.store import Store

ET = ZoneInfo("America/New_York")
REAL_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path: Path):
    """A throwaway repo root with the real config.toml, so tests exercise
    the shipped defaults rather than a hand-rolled stub that can drift."""
    shutil.copy(REAL_ROOT / "config.toml", tmp_path / "config.toml")
    (tmp_path / "data").mkdir()
    return load_config(tmp_path)


@pytest.fixture
def key() -> str:
    return generate_key()


PASSPHRASE = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def dashboard_passphrase(monkeypatch):
    """Every test gets a passphrase so the encrypted payload is always written."""
    monkeypatch.setenv("DASHBOARD_PASSPHRASE", PASSPHRASE)
    return PASSPHRASE


def read_payload(config, passphrase: str = PASSPHRASE) -> dict:
    """Decrypt what the hosted dashboard would download."""
    import json

    from coursetrack.publish import decrypt_payload

    return decrypt_payload(json.loads(config.payload_path.read_text()), passphrase)


@pytest.fixture
def store(config, key) -> Store:
    return Store(config, key=key).load()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 22, 9, 0, tzinfo=ET)


def make_deadline(
    *,
    title: str = "Homework 3",
    course: str = "17-437 Web Application Development",
    hours_out: float = 100,
    base: datetime | None = None,
    private: bool = False,
    source: str = "canvas",
) -> Deadline:
    base = base or datetime(2026, 9, 22, 9, 0, tzinfo=ET)
    return Deadline(
        id=canvas_id(12345, 98765) if source == "canvas" else manual_id(),
        source=source,
        course=course,
        course_id="12345" if source == "canvas" else "",
        title=title,
        due_at=base + timedelta(hours=hours_out),
        url="https://canvas.cmu.edu/courses/12345/assignments/98765",
        private=private,
    )
