"""Service account key parsing and JWT assertion signing.

The assertion is the whole credential: if it is malformed or mis-signed,
Google rejects it with a message that explains very little. These tests verify
the signature against the public half of a real generated keypair rather than
just checking the string has three dots in it.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from coursetrack.google.service_account import (
    SCOPE,
    TOKEN_URL,
    ServiceAccount,
    ServiceAccountError,
)

EMAIL = "coursetrack@proj-123.iam.gserviceaccount.com"


@pytest.fixture(scope="module")
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return private.public_key(), pem


@pytest.fixture
def key_json(keypair):
    _, pem = keypair
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "proj-123",
            "private_key_id": "abc",
            "private_key": pem,
            "client_email": EMAIL,
            "client_id": "12345",
        }
    )


def _unpad(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


# --- parsing ---------------------------------------------------------------


def test_parses_raw_json(key_json):
    account = ServiceAccount.parse(key_json)
    assert account.client_email == EMAIL
    assert "BEGIN PRIVATE KEY" in account.private_key


def test_parses_base64_json(key_json):
    """Base64 is the form that survives a .env line and a GitHub secret."""
    encoded = base64.b64encode(key_json.encode()).decode()
    assert ServiceAccount.parse(encoded).client_email == EMAIL


def test_tolerates_surrounding_whitespace(key_json):
    encoded = base64.b64encode(key_json.encode()).decode()
    assert ServiceAccount.parse(f"  \n{encoded}\n  ").client_email == EMAIL


def test_empty_value_explains_how_to_produce_one():
    with pytest.raises(ServiceAccountError, match="base64"):
        ServiceAccount.parse("")


def test_rejects_an_oauth_client_file_with_a_useful_message():
    """A very easy file to grab by mistake from the same console page."""
    wrong = json.dumps({"installed": {"client_id": "x"}, "type": "authorized_user"})
    with pytest.raises(ServiceAccountError, match="Expected a service account key"):
        ServiceAccount.parse(wrong)


def test_rejects_key_missing_client_email(keypair):
    _, pem = keypair
    blob = json.dumps({"type": "service_account", "private_key": pem})
    with pytest.raises(ServiceAccountError, match="client_email"):
        ServiceAccount.parse(blob)


def test_rejects_garbage():
    with pytest.raises(ServiceAccountError, match="neither JSON nor valid base64"):
        ServiceAccount.parse("not base64 and not json !!!")


def test_mangled_private_key_names_the_likely_cause():
    """Pasting raw JSON into .env destroys the newlines inside private_key."""
    blob = json.dumps(
        {
            "type": "service_account",
            "client_email": EMAIL,
            "private_key": "-----BEGIN PRIVATE KEY----- AAAA -----END PRIVATE KEY-----",
        }
    )
    with pytest.raises(ServiceAccountError, match="newlines"):
        ServiceAccount.parse(blob).assertion()


# --- assertion -------------------------------------------------------------


def test_assertion_has_the_claims_google_requires(key_json):
    account = ServiceAccount.parse(key_json)
    header_b64, claims_b64, _ = account.assertion(now=1_700_000_000).split(".")

    assert json.loads(_unpad(header_b64)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_unpad(claims_b64))
    assert claims["iss"] == EMAIL
    assert claims["scope"] == SCOPE
    assert claims["aud"] == TOKEN_URL
    assert claims["iat"] == 1_700_000_000
    assert claims["exp"] == 1_700_000_000 + 3600


def test_assertion_expiry_never_exceeds_one_hour(key_json):
    """Google rejects assertions with a longer lifetime."""
    _, claims_b64, _ = ServiceAccount.parse(key_json).assertion(now=0).split(".")
    claims = json.loads(_unpad(claims_b64))
    assert claims["exp"] - claims["iat"] <= 3600


def test_assertion_signature_verifies_against_the_public_key(key_json, keypair):
    public_key, _ = keypair
    token = ServiceAccount.parse(key_json).assertion(now=1_700_000_000)
    header_b64, claims_b64, signature_b64 = token.split(".")

    # Raises InvalidSignature if the signing input or algorithm is wrong.
    public_key.verify(
        _unpad(signature_b64),
        f"{header_b64}.{claims_b64}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_assertion_is_base64url_with_no_padding(key_json):
    """'+', '/' or '=' in a JWT segment means Google will reject it."""
    token = ServiceAccount.parse(key_json).assertion()
    for segment in token.split("."):
        assert "=" not in segment
        assert "+" not in segment
        assert "/" not in segment


def test_the_private_key_never_appears_in_the_assertion(key_json):
    account = ServiceAccount.parse(key_json)
    token = account.assertion()
    assert "PRIVATE KEY" not in token
    assert account.private_key not in token


def test_scope_is_calendar_only():
    """Nothing here should be able to read mail, drive or contacts."""
    assert SCOPE == "https://www.googleapis.com/auth/calendar"
