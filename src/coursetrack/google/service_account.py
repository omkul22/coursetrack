"""Service-account auth: a signed JWT exchanged for an access token.

Chosen over the OAuth user flow because this job runs unattended. There is no
consent screen, no app verification, and no refresh token to expire — the
credential is a keypair, and it works forever until you delete it.

It is also tighter: an OAuth grant would cover *every* calendar in the account,
whereas a service account can reach only the calendars explicitly shared with
its email address.

Signing is RS256 via `cryptography`, which the project already depends on for
state encryption, so this adds no new dependency.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
SCOPE = "https://www.googleapis.com/auth/calendar"

# Google rejects assertions with a lifetime over an hour.
ASSERTION_TTL = 3600


class ServiceAccountError(RuntimeError):
    pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass(frozen=True, slots=True)
class ServiceAccount:
    client_email: str
    private_key: str

    @classmethod
    def parse(cls, blob: str) -> ServiceAccount:
        """Accept the key file as raw JSON or base64-encoded JSON.

        The raw file contains literal newlines inside `private_key`, which do
        not survive a .env line or a shell round trip. Base64 makes it one
        safe line that behaves identically locally and in GitHub secrets.
        """
        blob = (blob or "").strip()
        if not blob:
            raise ServiceAccountError(
                "No service account key found. Set GOOGLE_SERVICE_ACCOUNT_JSON "
                "to the base64 of your key file:\n"
                "  base64 -i ~/Downloads/coursetrack-*.json | tr -d '\\n'"
            )

        if not blob.startswith("{"):
            try:
                blob = base64.b64decode(blob, validate=True).decode()
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise ServiceAccountError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON is neither JSON nor valid base64."
                ) from exc

        try:
            document = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise ServiceAccountError(f"Service account key is not valid JSON: {exc}") from exc

        if document.get("type") != "service_account":
            raise ServiceAccountError(
                f"Expected a service account key, got type={document.get('type')!r}. "
                "Download the key from IAM -> Service Accounts -> Keys -> Add key -> JSON."
            )
        for field in ("client_email", "private_key"):
            if not document.get(field):
                raise ServiceAccountError(f"Service account key is missing {field!r}.")

        return cls(client_email=document["client_email"], private_key=document["private_key"])

    def assertion(self, now: int | None = None) -> str:
        """A signed JWT proving we hold the service account's private key."""
        issued = int(now if now is not None else time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": self.client_email,
            "scope": SCOPE,
            "aud": TOKEN_URL,
            "iat": issued,
            "exp": issued + ASSERTION_TTL,
        }
        signing_input = (
            f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
            f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
        )

        try:
            key = serialization.load_pem_private_key(self.private_key.encode(), password=None)
        except ValueError as exc:
            raise ServiceAccountError(
                "Could not read the service account private key. If you pasted the JSON "
                "directly into .env, the newlines inside private_key were probably lost — "
                "use the base64 form instead."
            ) from exc

        signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64url(signature)}"


class TokenProvider:
    """Mints an access token and reuses it for the rest of the run."""

    def __init__(self, account: ServiceAccount, *, client: httpx.Client | None = None) -> None:
        self.account = account
        self._client = client
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token

        client = self._client or httpx.Client(timeout=30.0)
        try:
            response = client.post(
                TOKEN_URL,
                data={"grant_type": JWT_BEARER, "assertion": self.account.assertion()},
            )
        finally:
            if self._client is None:
                client.close()

        if response.status_code != 200:
            raise ServiceAccountError(_explain(response))

        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._token


def _explain(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error", "")
        detail = body.get("error_description", "")
    except ValueError:
        error, detail = response.text[:200], ""

    if error == "invalid_grant":
        return (
            "Google rejected the service account assertion (invalid_grant). Usual causes: "
            "the machine clock is badly skewed, or the key was deleted in the Cloud console. "
            f"{detail}"
        )
    if error == "invalid_client":
        return (
            "Google does not recognise this service account (invalid_client). Check the key "
            f"belongs to the right project and has not been deleted. {detail}"
        )
    return f"Service account token request failed ({response.status_code}): {error} {detail}".strip()
