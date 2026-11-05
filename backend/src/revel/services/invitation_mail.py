"""Invitation-only SMTP adapter. Disabled until an approved provider/sender is configured.

Implicit TLS (SMTP_SSL, certificate and hostname verification) is mandatory. Reservations
in the invitation service prevent duplicate sends; SMTP has no idempotency API, so uncertain
outcomes must not be retried automatically. No addresses, tokens or credentials are logged.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from functools import partial
from urllib.parse import urlsplit

import anyio.to_thread

from revel.settings import Settings


class SMTPInvitationMailer:
    def __init__(self, settings: Settings):
        from revel.services.invitations import MailUnavailable, normalize_email

        if not all(
            (
                settings.invitation_smtp_host,
                settings.invitation_smtp_username,
                settings.invitation_smtp_password,
                settings.invitation_from_email,
            )
        ):
            raise MailUnavailable()
        self._host = settings.invitation_smtp_host
        self._port = settings.invitation_smtp_port
        self._username = settings.invitation_smtp_username
        self._password = settings.invitation_smtp_password.get_secret_value()
        self._sender = normalize_email(settings.invitation_from_email)
        self._origin = settings.public_origin

    def _send(self, destination: str, invite_url: str, idempotency_key: str) -> None:
        from revel.services.invitations import MailUnavailable, normalize_email

        destination = normalize_email(destination)
        parts = urlsplit(invite_url)
        if f"{parts.scheme}://{parts.netloc}" != self._origin or parts.scheme != "https":
            raise MailUnavailable()
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = destination
        message["Subject"] = "You're invited to Revel Gifts"
        message["Message-ID"] = f"<{idempotency_key}@{self._sender.split('@')[1]}>"
        message.set_content(
            "Someone invited you to connect on Revel Gifts.\n\n"
            "Open the link, sign in with this email through WorkOS, then accept or decline:\n"
            + invite_url
            + "\n\n"
            "This invitation expires in seven days. Accepting grants no profile access. "
            "Sharing permissions are approved separately. If you weren't expecting this "
            "invitation, you can ignore it.\n"
        )
        with smtplib.SMTP_SSL(
            self._host, self._port, timeout=10, context=ssl.create_default_context()
        ) as client:
            client.login(self._username, self._password)
            client.send_message(message, from_addr=self._sender, to_addrs=[destination])

    async def send_invitation(
        self, *, destination: str, invite_url: str, idempotency_key: str
    ) -> None:
        await anyio.to_thread.run_sync(
            partial(self._send, destination, invite_url, idempotency_key)
        )


def configured_mailer(settings: Settings):
    from revel.services.invitations import DisabledMailer

    if settings.invitation_mail_provider == "smtp":
        return SMTPInvitationMailer(settings)
    return DisabledMailer()
