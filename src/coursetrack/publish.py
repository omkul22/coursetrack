"""Build the encrypted payload the hosted dashboard downloads.

The repo is public so GitHub Pages works on a free plan, which means this file
is world-readable. It is therefore AES-256-GCM ciphertext under a key derived
from a passphrase only you know.

Format is deliberately WebCrypto-native rather than reusing the Fernet blob
used for server state: `crypto.subtle` speaks PBKDF2 + AES-GCM directly, so
the browser needs no crypto library at all.

Threat model, stated plainly: the ciphertext is permanently public and can be
archived, so its security rests entirely on passphrase strength. A high
iteration count raises the cost of each offline guess but cannot rescue a weak
passphrase. Nothing here is a secret that grants access to anything else — no
tokens, no keys, only coursework titles and dates.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .models import Deadline
from .sources.base import is_done

FORMAT_VERSION = 1

# OWASP's 2023 floor for PBKDF2-SHA256. Roughly a second in a phone browser,
# and it multiplies the cost of every offline guess by the same factor.
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32  # AES-256

ENV_PASSPHRASE = "DASHBOARD_PASSPHRASE"
MIN_PASSPHRASE_LENGTH = 12


class PublishError(RuntimeError):
    pass


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def derive_key(passphrase: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(passphrase.encode())


def load_passphrase() -> str:
    passphrase = os.environ.get(ENV_PASSPHRASE, "").strip()
    if not passphrase:
        raise PublishError(
            f"${ENV_PASSPHRASE} is not set. This is the passphrase that unlocks the "
            "hosted dashboard; generate one with `coursetrack dashboard-key`."
        )
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise PublishError(
            f"${ENV_PASSPHRASE} is only {len(passphrase)} characters. The ciphertext is "
            f"published publicly, so short passphrases can be brute-forced offline. "
            f"Use at least {MIN_PASSPHRASE_LENGTH}."
        )
    return passphrase


@dataclass(frozen=True, slots=True)
class Payload:
    """What the browser decrypts and renders."""

    generated_at: datetime
    deadlines: list[Deadline]
    submissions: Mapping[str, str]
    dismissed: Iterable[str]
    courses: Mapping[str, str]
    thresholds: tuple[int, ...]
    reminder_ledger: Mapping[str, Mapping[str, str]]

    def to_dict(self) -> dict[str, Any]:
        dismissed = set(self.dismissed)
        rows = []
        for deadline in sorted(self.deadlines, key=lambda d: d.due_at):
            state = self.submissions.get(deadline.id, "")
            rows.append(
                {
                    "id": deadline.id,
                    "source": deadline.source,
                    # Real titles: the whole payload is encrypted, so there is
                    # nothing to gain by redacting private entries here.
                    "course": deadline.course,
                    "title": deadline.title,
                    "due_at": deadline.due_at.isoformat(),
                    "url": deadline.url,
                    "private": deadline.private,
                    "done": is_done(state),
                    "dismissed": deadline.id in dismissed,
                    "reminders": sorted(
                        self.reminder_ledger.get(deadline.id, {}), key=int, reverse=True
                    ),
                }
            )
        return {
            "generated_at": self.generated_at.isoformat(),
            "thresholds": list(self.thresholds),
            "courses": dict(self.courses),
            "deadlines": rows,
        }


def encrypt_payload(payload: Payload, passphrase: str) -> dict[str, Any]:
    """Envelope the payload so `crypto.subtle` can open it with no library."""
    plaintext = json.dumps(payload.to_dict(), separators=(",", ":")).encode()

    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    key = derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)

    return {
        "v": FORMAT_VERSION,
        "kdf": "PBKDF2-SHA256",
        "iterations": PBKDF2_ITERATIONS,
        "cipher": "AES-256-GCM",
        "salt": _b64(salt),
        "iv": _b64(nonce),
        "ct": _b64(ciphertext),
    }


def decrypt_payload(envelope: Mapping[str, Any], passphrase: str) -> dict[str, Any]:
    """Inverse of `encrypt_payload`. Exists so tests can prove the round trip."""
    key = derive_key(
        passphrase,
        base64.b64decode(envelope["salt"]),
        int(envelope.get("iterations", PBKDF2_ITERATIONS)),
    )
    plaintext = AESGCM(key).decrypt(
        base64.b64decode(envelope["iv"]), base64.b64decode(envelope["ct"]), None
    )
    return json.loads(plaintext)


def envelope_bytes(envelope: Mapping[str, Any]) -> bytes:
    return (json.dumps(envelope, indent=2) + "\n").encode()
