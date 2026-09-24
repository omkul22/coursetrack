"""SMTP delivery via Gmail.

`smtplib` is stdlib, and an app password does not expire — between them that
removes both a dependency and the gmail.send scope from the OAuth consent
screen. See the plan's "two findings" section for why that mattered.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class EmailError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Mailer:
    address: str
    app_password: str
    recipient: str
    dry_run: bool = False

    def send(self, subject: str, text: str, html: str | None = None) -> bool:
        message = EmailMessage()
        message["From"] = f"CourseTrack <{self.address}>"
        message["To"] = self.recipient
        message["Subject"] = subject
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")

        if self.dry_run:
            print(f"\n--- would send to {self.recipient} ---")
            print(f"Subject: {subject}\n")
            print(text)
            print("--- end ---\n")
            return False

        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.address, self.app_password)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailError(
                "Gmail rejected the login. Confirm 2-Step Verification is on and that "
                "GMAIL_APP_PASSWORD is a 16-character app password from "
                "https://myaccount.google.com/apppasswords (not your account password)."
            ) from exc
        except OSError as exc:
            raise EmailError(f"Could not reach {SMTP_HOST}: {exc}") from exc

        log.info("sent %r to %s", subject, self.recipient)
        return True


def build_mailer(secrets, recipient: str, dry_run: bool = False) -> Mailer:
    if not recipient and not dry_run:
        raise EmailError(
            "No recipient configured. Run `coursetrack set-email you@example.com`."
        )
    if not dry_run:
        # A dry run should be able to preview messages before any credential exists.
        secrets.require("gmail_address", "gmail_app_password")
    return Mailer(
        address=secrets.gmail_address,
        app_password=secrets.gmail_app_password,
        recipient=recipient,
        dry_run=dry_run,
    )
