"""Persistence. Two files, both ciphertext, both committed to a public repo.

    data/private.enc   Fernet under COURSETRACK_KEY.
                       Server-side state: manual entries, the Canvas cache,
                       submission states, the reminder ledger, calendar event
                       ids, course settings, your email address.

    docs/data.enc      AES-256-GCM under DASHBOARD_PASSPHRASE.
                       What the hosted dashboard downloads and decrypts in the
                       browser. Same deadlines, different key, different
                       audience.

There is deliberately no cleartext file. An earlier version published course
names and assignment titles in the clear, which was tolerable while the repo
was private and became unacceptable the moment Pages required it to be public.
Removing the cleartext path entirely is safer than maintaining a redaction
function nobody remembers to update.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .crypto import decrypt_json, encrypt_json, load_key
from .models import CANVAS, MANUAL, Deadline

SCHEMA_VERSION = 2


@dataclass
class PrivateState:
    """Everything the scheduled job needs to remember between runs."""

    version: int = SCHEMA_VERSION
    recipient_email: str = ""

    # deadline_id -> {"72": "<iso sent at>", "12": "<iso sent at>"}
    reminder_ledger: dict[str, dict[str, str]] = field(default_factory=dict)

    # deadline_id -> google calendar event id (a cache; the authoritative
    # link is extendedProperties.private.coursetrack_id on the event itself)
    calendar_event_ids: dict[str, str] = field(default_factory=dict)
    calendar_id: str = ""

    # deadline_id -> canvas submission workflow_state
    submission_states: dict[str, str] = field(default_factory=dict)

    # Manually added deadlines. The encrypted file is their only source.
    manual_entries: list[dict[str, Any]] = field(default_factory=list)

    # Last Canvas fetch, so the dashboard and CLI can render offline.
    canvas_cache: list[dict[str, Any]] = field(default_factory=list)

    # course_id -> {"enabled": bool, "name": str}
    course_settings: dict[str, dict[str, Any]] = field(default_factory=dict)

    dismissed: list[str] = field(default_factory=list)

    last_brief_date: str = ""
    last_heartbeat_date: str = ""
    last_sync_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PrivateState:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass(frozen=True, slots=True)
class SaveResult:
    private_changed: bool
    payload_changed: bool

    @property
    def any_changed(self) -> bool:
        return self.private_changed or self.payload_changed


def _to_row(deadline: Deadline) -> dict[str, Any]:
    return {
        "id": deadline.id,
        "source": deadline.source,
        "course": deadline.course,
        "course_id": deadline.course_id,
        "title": deadline.title,
        "due_at": deadline.due_at.isoformat(),
        "url": deadline.url,
        "private": deadline.private,
    }


def _from_row(row: dict[str, Any]) -> Deadline:
    return Deadline(
        id=row["id"],
        source=row.get("source", CANVAS),
        course=row.get("course", ""),
        course_id=row.get("course_id", ""),
        title=row.get("title", ""),
        due_at=datetime.fromisoformat(row["due_at"]),
        url=row.get("url"),
        private=bool(row.get("private", False)),
    )


class Store:
    def __init__(self, config: Config, key: str | None = None) -> None:
        self.config = config
        self._key = key
        self.private = PrivateState()
        self._private_snapshot = ""

    @property
    def key(self) -> str:
        if self._key is None:
            self._key = load_key()
        return self._key

    # ---- load ------------------------------------------------------------

    def load(self) -> Store:
        path = self.config.private_path
        if path.is_file() and path.stat().st_size > 0:
            self.private = PrivateState.from_dict(decrypt_json(self.key, path.read_bytes()))
        else:
            self.private = PrivateState()
        self._private_snapshot = self._canonical_private()
        return self

    def _canonical_private(self) -> str:
        """Stable text form of the decrypted state, for change detection.

        Fernet embeds a random IV and a timestamp, so encrypting identical
        content twice produces different ciphertext. Comparing ciphertext
        would report a change on every run and commit hourly forever. Compare
        the plaintext instead. The same applies to the AES-GCM payload, which
        gets a fresh salt and nonce each time.
        """
        return json.dumps(asdict(self.private), sort_keys=True, default=str)

    # ---- deadlines -------------------------------------------------------

    def manual_deadlines(self) -> list[Deadline]:
        return [_from_row(row) for row in self.private.manual_entries]

    def cached_deadlines(self) -> list[Deadline]:
        """Canvas rows from the last sync."""
        return [_from_row(row) for row in self.private.canvas_cache]

    def all_deadlines(self) -> list[Deadline]:
        """Manual entries plus the last Canvas fetch.

        Any write that is not a full sync must persist through this, or it
        republishes from manual entries alone and drops every Canvas deadline.
        """
        return [*self.manual_deadlines(), *self.cached_deadlines()]

    def set_canvas_cache(self, deadlines: Iterable[Deadline]) -> None:
        self.private.canvas_cache = [
            _to_row(d) for d in deadlines if d.source != MANUAL
        ]

    def add_manual(self, deadline: Deadline) -> None:
        self.private.manual_entries.append(_to_row(deadline))

    def remove_manual(self, deadline_id: str) -> bool:
        before = len(self.private.manual_entries)
        self.private.manual_entries = [
            e for e in self.private.manual_entries if e["id"] != deadline_id
        ]
        return len(self.private.manual_entries) != before

    # ---- course settings -------------------------------------------------

    def course_enabled(self, course_id: str) -> bool:
        return bool(self.private.course_settings.get(str(course_id), {}).get("enabled", True))

    def set_course_enabled(self, course_id: str, enabled: bool) -> None:
        self.private.course_settings.setdefault(str(course_id), {})["enabled"] = enabled

    def enabled_course_names(self) -> dict[str, str]:
        return {
            cid: meta.get("name", cid)
            for cid, meta in self.private.course_settings.items()
            if meta.get("enabled", True)
        }

    # ---- save ------------------------------------------------------------

    def save(
        self,
        deadlines: Iterable[Deadline] | None,
        generated_at: datetime,
        *,
        force_payload: bool = False,
        dry_run: bool = False,
        passphrase: str | None = None,
    ) -> SaveResult:
        """Persist encrypted state, and rebuild the browser payload.

        `deadlines` is what to publish; None means "whatever is already
        stored", which is what non-sync writes want.
        """
        from .publish import Payload, PublishError, encrypt_payload, envelope_bytes, load_passphrase

        rows = list(deadlines) if deadlines is not None else self.all_deadlines()
        private_changed = self._canonical_private() != self._private_snapshot

        payload = Payload(
            generated_at=generated_at,
            deadlines=rows,
            submissions=self.private.submission_states,
            dismissed=self.private.dismissed,
            courses=self.enabled_course_names(),
            thresholds=self.config.threshold_hours,
            reminder_ledger=self.private.reminder_ledger,
        )
        document = payload.to_dict()
        payload_changed = self._payload_differs(document) or force_payload

        if dry_run:
            return SaveResult(private_changed, payload_changed)

        self.config.data_dir.mkdir(parents=True, exist_ok=True)

        if private_changed:
            _atomic_write(self.config.private_path, encrypt_json(self.key, asdict(self.private)))
            self._private_snapshot = self._canonical_private()

        if payload_changed:
            try:
                secret = passphrase or load_passphrase()
            except PublishError:
                # Encryption of the hosted payload is optional locally; the
                # scheduled job sets the passphrase and will publish it.
                return SaveResult(private_changed, False)
            self.config.payload_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(
                self.config.payload_path,
                envelope_bytes(encrypt_payload(payload, secret)),
            )
            _atomic_write(self.config.payload_plain_cache, json.dumps(document).encode())

        return SaveResult(private_changed, payload_changed)

    def _payload_differs(self, document: dict[str, Any]) -> bool:
        """Compare the plaintext payload, ignoring the timestamp.

        The ciphertext changes every run by construction (fresh salt/nonce),
        so a local plaintext cache is kept purely to answer "did anything
        actually change?" without decrypting.
        """
        cache = self.config.payload_plain_cache
        if not self.config.payload_path.is_file() or not cache.is_file():
            return True
        try:
            previous = json.loads(cache.read_text())
        except json.JSONDecodeError:
            return True
        return _comparable(previous) != _comparable(document)


def _comparable(document: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in document.items() if k != "generated_at"}


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write via a temp file + rename so an interrupted run cannot truncate state."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)
