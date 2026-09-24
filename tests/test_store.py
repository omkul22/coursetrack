"""Encrypted persistence: both files, change detection, and no cleartext.

This repo is public, so the tests that matter most here are the ones proving
nothing readable is ever written to disk.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from coursetrack.crypto import DecryptionError, decrypt_json, encrypt_json, generate_key
from coursetrack.models import Deadline, manual_id
from coursetrack.publish import decrypt_payload
from coursetrack.store import Store

from .conftest import PASSPHRASE, make_deadline, read_payload


# --- nothing readable on disk ----------------------------------------------


def test_no_cleartext_file_is_ever_written(config, key, now):
    store = Store(config, key=key).load()
    store.add_manual(make_deadline(source="manual", title="Visa appointment", private=True))
    store.save([make_deadline(title="Homework 3")], now)

    assert not (config.data_dir / "deadlines.json").exists()

    # Everything committed must be unreadable.
    for path in (config.private_path, config.payload_path):
        blob = path.read_bytes()
        for secret in (b"Homework 3", b"Visa appointment", b"17-437"):
            assert secret not in blob, f"{secret!r} is readable in {path.name}"


def test_private_state_file_is_fernet(config, key, now):
    store = Store(config, key=key).load()
    store.set_canvas_cache([make_deadline()])  # private.enc is only written when it changes
    store.save(None, now)
    assert config.private_path.read_bytes().startswith(b"gAAAAA")


def test_payload_envelope_declares_real_crypto(config, key, now):
    Store(config, key=key).load().save([make_deadline()], now)
    envelope = json.loads(config.payload_path.read_text())

    assert envelope["cipher"] == "AES-256-GCM"
    assert envelope["kdf"] == "PBKDF2-SHA256"
    assert envelope["iterations"] >= 600_000
    assert set(envelope) >= {"v", "salt", "iv", "ct"}


def test_payload_round_trips(config, key, now):
    store = Store(config, key=key).load()
    store.save([make_deadline(title="Homework 3", course="17-437")], now)

    payload = read_payload(config)
    assert payload["deadlines"][0]["title"] == "Homework 3"
    assert payload["deadlines"][0]["course"] == "17-437"


def test_wrong_passphrase_cannot_open_the_payload(config, key, now):
    Store(config, key=key).load().save([make_deadline()], now)
    envelope = json.loads(config.payload_path.read_text())

    with pytest.raises(Exception):
        decrypt_payload(envelope, "not-the-passphrase")


def test_private_deadlines_keep_real_titles_in_the_payload(config, key, now):
    """The payload is encrypted, so redaction would only hurt usefulness."""
    store = Store(config, key=key).load()
    store.add_manual(
        Deadline(
            id=manual_id(), source="manual", course="Personal", course_id="",
            title="Visa appointment", due_at=now + timedelta(days=2), private=True,
        )
    )
    store.save(None, now)

    row = read_payload(config)["deadlines"][0]
    assert row["title"] == "Visa appointment"
    assert row["private"] is True


# --- crypto ----------------------------------------------------------------


def test_fernet_round_trip(key):
    document = {"reminder_ledger": {"canvas:1:2": {"72": "2026-09-22T09:00:00-04:00"}}}
    assert decrypt_json(key, encrypt_json(key, document)) == document


def test_wrong_key_raises_rather_than_returning_garbage(key):
    token = encrypt_json(key, {"secret": "value"})
    with pytest.raises(DecryptionError):
        decrypt_json(generate_key(), token)


def test_tampered_ciphertext_is_rejected(key):
    """Anyone can open a PR against a public repo; a modified blob must fail."""
    token = bytearray(encrypt_json(key, {"secret": "value"}))
    token[-5] ^= 0xFF
    with pytest.raises(DecryptionError):
        decrypt_json(key, bytes(token))


# --- change detection ------------------------------------------------------


def test_unchanged_state_is_not_rewritten(config, key, now):
    """Both ciphertexts randomise, so this guards against an hourly no-op commit."""
    deadlines = [make_deadline()]
    store = Store(config, key=key).load()
    store.private.recipient_email = "someone@example.com"
    first = store.save(deadlines, now)
    assert first.private_changed and first.payload_changed

    second = Store(config, key=key).load().save(deadlines, now + timedelta(hours=1))
    assert not second.private_changed
    assert not second.payload_changed
    assert not second.any_changed


def test_the_timestamp_alone_is_not_a_change(config, key, now):
    deadlines = [make_deadline()]
    Store(config, key=key).load().save(deadlines, now)
    before = config.payload_path.read_bytes()

    result = Store(config, key=key).load().save(deadlines, now + timedelta(days=3))
    assert not result.payload_changed
    assert config.payload_path.read_bytes() == before


def test_force_payload_refreshes_for_the_heartbeat(config, key, now):
    deadlines = [make_deadline()]
    Store(config, key=key).load().save(deadlines, now)

    later = now + timedelta(hours=6)
    result = Store(config, key=key).load().save(deadlines, later, force_payload=True)
    assert result.payload_changed
    assert read_payload(config)["generated_at"] == later.isoformat()


def test_a_data_change_is_detected(config, key, now):
    Store(config, key=key).load().save([make_deadline()], now)
    result = Store(config, key=key).load().save(
        [make_deadline(), make_deadline(title="Quiz 2")], now
    )
    assert result.payload_changed


def test_private_state_survives_a_round_trip(config, key, now):
    store = Store(config, key=key).load()
    store.private.recipient_email = "omkul22@gmail.com"
    store.private.reminder_ledger["canvas:1:2"] = {"72": now.isoformat()}
    store.set_course_enabled("12345", False)
    store.save(None, now)

    reloaded = Store(config, key=key).load()
    assert reloaded.private.recipient_email == "omkul22@gmail.com"
    assert reloaded.private.reminder_ledger["canvas:1:2"]["72"] == now.isoformat()
    assert reloaded.course_enabled("12345") is False
    assert reloaded.course_enabled("99999") is True  # default-on


def test_canvas_cache_persists_encrypted(config, key, now):
    store = Store(config, key=key).load()
    store.set_canvas_cache([make_deadline(title="From Canvas")])
    store.save(None, now)

    reloaded = Store(config, key=key).load()
    assert [d.title for d in reloaded.cached_deadlines()] == ["From Canvas"]


def test_manual_entries_are_not_duplicated_into_the_canvas_cache(config, key, now):
    store = Store(config, key=key).load()
    store.add_manual(make_deadline(source="manual", title="Manual one"))
    store.set_canvas_cache(store.all_deadlines())

    assert store.private.canvas_cache == []
    assert len(store.all_deadlines()) == 1


# --- model invariants ------------------------------------------------------


def test_naive_datetime_is_rejected():
    """A naive due_at compared against an aware `now` is a silently late reminder."""
    with pytest.raises(ValueError, match="timezone-aware"):
        Deadline(
            id="manual:x", source="manual", course="c", course_id="", title="t",
            due_at=datetime(2026, 9, 25, 23, 59),
        )


def test_payload_is_sorted_by_due_date(config, key, now):
    store = Store(config, key=key).load()
    store.save(
        [make_deadline(title="late", hours_out=200), make_deadline(title="early", hours_out=2)],
        now,
    )
    assert [r["title"] for r in read_payload(config)["deadlines"]] == ["early", "late"]
