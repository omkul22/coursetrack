"""Fernet wrapper for the private half of the state.

Fernet is AES-128-CBC with an HMAC-SHA256 authentication tag, so a tampered
`private.enc` fails loudly rather than decrypting to garbage. That matters here
because the file lives in a public repo where anyone can open a PR against it.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

ENV_VAR = "COURSETRACK_KEY"
KEYCHAIN_SERVICE = "coursetrack"
KEYCHAIN_ACCOUNT = "state-key"


class KeyMissingError(RuntimeError):
    pass


class DecryptionError(RuntimeError):
    pass


def generate_key() -> str:
    """A fresh urlsafe-base64 key, suitable for the env var and the Keychain."""
    return Fernet.generate_key().decode()


def load_key() -> str:
    """Resolve the key from the environment, falling back to the macOS Keychain.

    The environment wins so GitHub Actions needs no special case, and so a
    test can override it without touching the developer's real Keychain.
    """
    key = os.environ.get(ENV_VAR)
    if key:
        return key.strip()

    key = keychain_get()
    if key:
        return key

    raise KeyMissingError(
        f"No encryption key found. Set ${ENV_VAR}, or store one in the Keychain with:\n"
        f"  coursetrack init-key"
    )


def keychain_get() -> str | None:
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return None  # Not macOS, or `security` unavailable.
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def keychain_set(key: str) -> None:
    """Store the key in the login Keychain, replacing any existing entry."""
    subprocess.run(
        [
            "security", "add-generic-password",
            "-s", KEYCHAIN_SERVICE,
            "-a", KEYCHAIN_ACCOUNT,
            "-w", key,
            "-U",  # update if it already exists
        ],
        check=True,
        capture_output=True,
    )


def encrypt_json(key: str, document: Any) -> bytes:
    payload = json.dumps(document, indent=2, sort_keys=True, default=str).encode()
    return Fernet(key.encode()).encrypt(payload)


def decrypt_json(key: str, token: bytes) -> Any:
    try:
        payload = Fernet(key.encode()).decrypt(token)
    except InvalidToken as exc:
        raise DecryptionError(
            "Could not decrypt data/private.enc. Either the key is wrong, or the "
            "file was modified by something other than coursetrack."
        ) from exc
    return json.loads(payload)
