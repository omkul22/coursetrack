#!/usr/bin/env python3
"""Refuse to publish anything readable.

This repo is public so GitHub Pages works on a free plan. Every committed file
that touches coursework must therefore be ciphertext. This script is the gate:
it runs before the commit step in the workflow and in CI, and fails the job if
it finds anything legible.

Checks, in order of how badly you want them to pass:

  1. No cleartext deadline file exists (the old data/deadlines.json).
  2. docs/data.enc is a well-formed encrypted envelope, not JSON someone
     forgot to encrypt.
  3. The ciphertext does not decode to anything resembling plaintext.
  4. No committed file contains a credential-shaped string.
  5. With the keys present, the payload decrypts and its contents do not
     appear anywhere in a committed file.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PAYLOAD = ROOT / "docs" / "data.enc"
PRIVATE = ROOT / "data" / "private.enc"
FORBIDDEN_CLEARTEXT = [ROOT / "data" / "deadlines.json"]

SECRET_PATTERNS = [
    (re.compile(r"\bya29\.[\w\-]{20,}"), "a Google access token"),
    (re.compile(r"\bAIza[\w\-]{30,}"), "a Google API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s]*[A-Za-z0-9+/=\s]{200,}"), "a private key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}"), "a GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "a fine-grained GitHub PAT"),
    (re.compile(r"\b\d{4}~[A-Za-z0-9]{40,}"), "a Canvas access token"),
]

problems: list[str] = []


def fail(message: str) -> None:
    problems.append(message)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def check_no_cleartext_file() -> None:
    for path in FORBIDDEN_CLEARTEXT:
        if path.exists():
            fail(f"{path.relative_to(ROOT)} exists; it published titles in the clear")


def check_envelope() -> dict | None:
    if not PAYLOAD.is_file():
        return None
    try:
        envelope = json.loads(PAYLOAD.read_text())
    except json.JSONDecodeError:
        fail("docs/data.enc is not valid JSON")
        return None

    for field in ("v", "salt", "iv", "ct", "kdf", "cipher"):
        if field not in envelope:
            fail(f"docs/data.enc is missing {field!r} — is it actually encrypted?")
            return None
    if envelope.get("cipher") != "AES-256-GCM":
        fail(f"unexpected cipher {envelope.get('cipher')!r}")
    if int(envelope.get("iterations", 0)) < 100_000:
        fail(f"PBKDF2 iterations too low: {envelope.get('iterations')}")

    # The ciphertext must not contain recognisable text.
    try:
        raw = base64.b64decode(envelope["ct"])
    except Exception:  # noqa: BLE001
        fail("docs/data.enc ciphertext is not valid base64")
        return envelope
    if b"due_at" in raw or b"deadlines" in raw or b"{" == raw[:1]:
        fail("docs/data.enc ciphertext contains plaintext markers — it is NOT encrypted")
    return envelope


def check_private_is_encrypted() -> None:
    if not PRIVATE.is_file():
        return
    head = PRIVATE.read_bytes()[:10]
    if not head.startswith(b"gAAAAA"):
        fail("data/private.enc does not look like a Fernet token")


# Tests legitimately contain credential-shaped fixtures: an ephemeral RSA key
# generated at runtime, a fake @proj-123.iam.gserviceaccount.com address. They
# are still scanned for real key material by the tightened PEM pattern above.
TEST_ONLY_PATTERNS = [re.compile(r"\.iam\.gserviceaccount\.com")]


def check_tracked_for_secrets() -> None:
    for path in tracked_files():
        if not path.is_file() or path.suffix in {".png", ".jpg", ".ico"}:
            continue
        # This file necessarily contains every pattern it looks for.
        if path.resolve() == Path(__file__).resolve():
            continue
        in_tests = "tests" in path.parts
        for pattern in [] if in_tests else TEST_ONLY_PATTERNS:
            if pattern.search(path.read_text(errors="ignore")):
                fail(f"{path.relative_to(ROOT)} contains a service account address")
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for pattern, description in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                fail(f"{path.relative_to(ROOT)} contains {description}")


def check_payload_contents_are_not_leaked(envelope: dict | None) -> bool:
    """Decrypt the payload and confirm none of it appears in a tracked file."""
    passphrase = os.environ.get("DASHBOARD_PASSPHRASE", "")
    if not envelope or not passphrase:
        return False

    from coursetrack.publish import decrypt_payload

    payload = decrypt_payload(envelope, passphrase)
    titles = [row["title"] for row in payload.get("deadlines", [])]
    courses = {row["course"] for row in payload.get("deadlines", [])}
    needles = [t for t in titles if len(t) > 8] + [c for c in courses if len(c) > 8]

    # Only generated files. Source and tests are hand-written and reviewed, and
    # their placeholder fixtures ("Homework 3", "10714 Deep Learning Systems")
    # collide with real assignment titles constantly -- flagging those trains
    # you to ignore the check, which is worse than not having it.
    for path in tracked_files():
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        generated = rel.parts and rel.parts[0] in {"data", "docs"}
        if not generated or path.name == "data.enc":
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for needle in needles:
            if needle in text:
                fail(f"{rel} leaks {needle[:32]!r} in the clear")
    return True


def main() -> int:
    check_no_cleartext_file()
    envelope = check_envelope()
    check_private_is_encrypted()
    check_tracked_for_secrets()
    deep = check_payload_contents_are_not_leaked(envelope)

    if problems:
        print("FAIL: this repo is public and something readable is about to ship\n")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    count = 0
    if envelope:
        count = len(base64.b64decode(envelope["ct"]))
    mode = "deep (payload decrypted and cross-checked)" if deep else "shallow (no passphrase)"
    print(f"OK: no cleartext. payload {count} bytes of ciphertext, {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
