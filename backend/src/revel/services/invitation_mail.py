"""Invitation-only SMTP adapter. Disabled until an approved provider/sender is configured.

Implicit TLS (SMTP_SSL, certificate and hostname verification) is mandatory. Reservations
in the invitation service prevent duplicate sends; SMTP has no idempotency API, so uncertain
outcomes must not be retried automatically. No addresses, tokens or credentials are logged.
"""

from __future__ import annotations

import smtplib
import ssl
from contextlib import asynccontextmanager
from email.message import EmailMessage
from functools import partial
from urllib.parse import urlsplit

import anyio
import anyio.to_thread

from revel.services.invitation_email import branded_sender, render_invitation
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

    def _send(
        self,
        destination: str,
        invite_url: str,
        idempotency_key: str,
        *,
        sender_name: str | None,
    ) -> None:
        from revel.services.invitations import MailUnavailable, normalize_email

        destination = normalize_email(destination)
        parts = urlsplit(invite_url)
        if f"{parts.scheme}://{parts.netloc}" != self._origin or parts.scheme != "https":
            raise MailUnavailable()
        message = EmailMessage()
        content = render_invitation(sender_name=sender_name, invite_url=invite_url)
        message["From"] = branded_sender(self._sender)
        message["To"] = destination
        message["Subject"] = content.subject
        message["Message-ID"] = f"<{idempotency_key}@{self._sender.split('@')[1]}>"
        message.set_content(content.text)
        message.add_alternative(content.html, subtype="html")
        with smtplib.SMTP_SSL(
            self._host, self._port, timeout=10, context=ssl.create_default_context()
        ) as client:
            client.login(self._username, self._password)
            client.send_message(message, from_addr=self._sender, to_addrs=[destination])

    async def send_invitation(
        self,
        *,
        destination: str,
        invite_url: str,
        idempotency_key: str,
        sender_name: str | None,
    ) -> None:
        await anyio.to_thread.run_sync(
            partial(
                self._send,
                destination,
                invite_url,
                idempotency_key,
                sender_name=sender_name,
            )
        )


def configured_mailer(settings: Settings):
    from revel.services.invitations import DisabledMailer

    if settings.invitation_mail_provider in ("smtp", "resend"):
        if not settings.invitation_background_delivery_ready:
            from revel.services.invitations import MailUnavailable

            raise MailUnavailable()
        if settings.invitation_mail_provider == "resend":
            from revel.services.resend_mail import ResendInvitationMailer

            return ResendInvitationMailer(settings)
        return SMTPInvitationMailer(settings)
    return DisabledMailer()


class MailDispatcher:
    """Application-lifespan task group, with observed failures and graceful job drain.

    This is deliberately at-most-once: a process crash leaves a durable `sending`
    reservation, which is never replayed automatically. It is not a durable mail queue.
    """

    def __init__(self):
        self._group = None
        self._pending = 0
        self._idle = anyio.Event()
        self._idle.set()

    @property
    def running(self) -> bool:
        return self._group is not None

    @asynccontextmanager
    async def run(self):
        async with anyio.create_task_group() as group:
            self._group = group
            try:
                yield self
            finally:
                self._group = None

    def enqueue(self, job) -> None:
        if self._group is None:
            raise RuntimeError("invitation dispatcher is not running")
        if self._pending == 0:
            self._idle = anyio.Event()
        self._pending += 1
        self._group.start_soon(self._observe, job)

    async def _observe(self, job) -> None:
        try:
            await job()
        except Exception:
            # The worker records unknown when possible. Never expose provider errors or
            # private account/block evidence to a request, logs, or the task-group caller.
            pass
        finally:
            self._pending -= 1
            if self._pending == 0:
                self._idle.set()

    async def wait_idle(self) -> None:
        await self._idle.wait()


_dispatchers: dict[int, MailDispatcher] = {}


def dispatcher_for(settings: Settings) -> MailDispatcher | None:
    return _dispatchers.get(id(settings))


@asynccontextmanager
async def dispatcher_lifespan(settings: Settings):
    dispatcher = MailDispatcher()
    async with dispatcher.run():
        _dispatchers[id(settings)] = dispatcher
        try:
            yield dispatcher
        finally:
            _dispatchers.pop(id(settings), None)
