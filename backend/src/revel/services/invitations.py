"""Email invitations never grant a sharing tier. Recipient acceptance creates a request only.

Creation never looks up destination accounts. Redemption uses a fresh verified WorkOS email,
not cached users.email. Capability material is sent in JSON bodies and URL fragments only.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID

import anyio.to_thread
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from revel.auth.browser import CookieSealer
from revel.auth.identity import Principal
from revel.db.models import ConnectionRequest, Invitation, User
from revel.services import connections
from revel.settings import Settings, get_settings

LIFETIME = timedelta(days=7)
CREATE_LIMIT = 20
SEND_LIMIT = 20
# Deliberately a single ASCII dot-atom mailbox, never display-name/comments/routes.
# SMTP's address parser must not reinterpret the destination used for email binding/blocks.
_EMAIL = re.compile(
    r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


class InvitationError(Exception):
    code = "invalid_invitation"
    status = 409
    message = "This invitation is unavailable or no longer pending."


class EmailVerificationRequired(InvitationError):
    code = "invited_email_required"
    status = 403
    message = "Sign in through WorkOS with the invited email and verify it, then try again."


class ConfirmationRequired(InvitationError):
    code = "owner_confirmation_required"
    status = 403
    message = "Confirm the destination email and that you want to send this invitation."


class InvalidEmail(InvitationError):
    code = "invalid_email"
    status = 422
    message = "Enter a valid email address."


class RateLimited(InvitationError):
    code = "rate_limited"
    status = 429
    message = "Too many invitations; try again later."


class Unavailable(InvitationError):
    code = "unavailable"
    status = 503
    message = "Invitations are temporarily unavailable; please retry."


class MailUnavailable(Unavailable):
    code = "mail_unavailable"
    message = "Email delivery is not configured. Copy the invitation link instead."


class InvitationInfo(BaseModel):
    id: UUID
    email: str
    status: str
    expires_at: datetime
    delivery_status: str
    invite_url: str | None = None
    connection_request_id: UUID | None = None


class InvitationPreview(BaseModel):
    sender_name: str | None
    expires_at: datetime
    status: str
    sharing_notice: str = "Accepting creates a connection request. No profile access is granted."


class Mailer(Protocol):
    async def send_invitation(
        self, *, destination: str, invite_url: str, idempotency_key: str
    ) -> None: ...


class DisabledMailer:
    async def send_invitation(self, **kwargs: Any) -> None:
        raise MailUnavailable()


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    local = value.partition("@")[0]
    if (
        len(value) > 254
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not _EMAIL.fullmatch(value)
    ):
        raise InvalidEmail()
    return value


def _sealer(settings: Settings) -> CookieSealer:
    if settings.workos_cookie_password is None:
        raise Unavailable()
    return CookieSealer(settings.workos_cookie_password.get_secret_value())


def _token_hash(token: str) -> str:
    if not isinstance(token, str) or len(token) > 256 or len(token) < 32:
        raise InvitationError()
    return hashlib.sha256(token.encode()).hexdigest()


def _info(row: Invitation, settings: Settings, *, link: bool = False) -> InvitationInfo:
    url = None
    if link and row.status == "pending":
        try:
            token = _sealer(settings).unseal("invitation", row.sealed_token)["token"]
        except Exception:
            raise Unavailable() from None
        url = settings.public_base_url.rstrip("/") + "/app/invite#token=" + token
    status = (
        "expired" if row.status == "pending" and row.expires_at <= datetime.now(UTC) else row.status
    )
    return InvitationInfo(
        id=row.id,
        email=row.email,
        status=status,
        expires_at=row.expires_at,
        delivery_status=("submitted" if row.delivery_status != "not_sent" else "not_sent"),
        invite_url=url,
        connection_request_id=row.connection_request_id,
    )


async def _run(fn: Callable[[], Any]) -> Any:
    try:
        return await anyio.to_thread.run_sync(fn)
    except InvitationError:
        raise
    except Exception:
        raise Unavailable() from None


def _start(session: Session, principal: Principal) -> None:
    connections._begin(session)
    connections._check_actor(session, principal)


async def create(
    principal: Principal,
    *,
    email: str,
    channel: Literal["copy", "email"] = "copy",
    owner_confirmed: bool = False,
    settings: Settings | None = None,
    sessions: Any = None,
    mailer: Mailer | None = None,
) -> InvitationInfo:
    settings = settings or get_settings()
    email = normalize_email(email)
    if channel not in ("copy", "email"):
        raise InvitationError()
    if channel == "email" and not owner_confirmed:
        raise ConfirmationRequired()
    factory = connections._factory(sessions)

    def work() -> InvitationInfo:
        with factory() as session, session.begin():
            _start(session, principal)
            connections._advisory_lock(
                session, connections._lock_key("revel.invites.sender", principal.user_id)
            )
            now = session.scalar(select(func.now()))
            session.execute(
                update(Invitation)
                .where(
                    Invitation.sender_user_id == principal.user_id,
                    Invitation.status == "pending",
                    Invitation.expires_at <= now,
                )
                .values(status="expired")
            )
            existing = session.scalar(
                select(Invitation).where(
                    Invitation.sender_user_id == principal.user_id,
                    Invitation.email == email,
                    Invitation.status == "pending",
                )
            )
            if existing:
                return _info(existing, settings, link=True)
            count = session.scalar(
                select(func.count())
                .select_from(Invitation)
                .where(
                    Invitation.sender_user_id == principal.user_id,
                    Invitation.created_at > now - timedelta(hours=1),
                )
            )
            if count >= CREATE_LIMIT:
                raise RateLimited()
            token = secrets.token_urlsafe(32)
            row = Invitation(
                sender_user_id=principal.user_id,
                email=email,
                token_hash=_token_hash(token),
                sealed_token=_sealer(settings).seal("invitation", {"token": token}),
                status="pending",
                expires_at=now + LIFETIME,
                delivery_status="not_sent",
            )
            session.add(row)
            session.flush()
            return _info(row, settings, link=True)

    result = await _run(work)
    if channel == "email":
        return await send(
            principal,
            result.id,
            owner_confirmed=True,
            settings=settings,
            sessions=sessions,
            mailer=mailer,
        )
    return result


async def list_sent(
    principal: Principal, *, sessions: Any = None, settings: Settings | None = None
) -> list[InvitationInfo]:
    settings = settings or get_settings()
    factory = connections._factory(sessions)

    def work() -> list[InvitationInfo]:
        with factory() as session:
            _start(session, principal)
            rows = session.scalars(
                select(Invitation)
                .where(Invitation.sender_user_id == principal.user_id)
                .order_by(Invitation.created_at.desc())
                .limit(200)
            )
            return [_info(row, settings) for row in rows]

    return await _run(work)


async def cancel(
    principal: Principal,
    invitation_id: UUID,
    *,
    sessions: Any = None,
    settings: Settings | None = None,
) -> InvitationInfo:
    settings = settings or get_settings()
    factory = connections._factory(sessions)

    def work() -> InvitationInfo:
        with factory() as session, session.begin():
            _start(session, principal)
            row = session.scalar(
                select(Invitation)
                .where(
                    Invitation.id == invitation_id, Invitation.sender_user_id == principal.user_id
                )
                .with_for_update()
            )
            if row is None or row.status != "pending":
                raise InvitationError()
            row.status = "cancelled"
            return _info(row, settings)

    return await _run(work)


async def send(
    principal: Principal,
    invitation_id: UUID,
    *,
    owner_confirmed: bool = False,
    sessions: Any = None,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
    directory: Any = None,
) -> InvitationInfo:
    if not owner_confirmed:
        raise ConfirmationRequired()
    settings = settings or get_settings()
    if mailer is None:
        from revel.services.invitation_mail import configured_mailer

        mailer = configured_mailer(settings)
    if isinstance(mailer, DisabledMailer):
        raise MailUnavailable()
    factory = connections._factory(sessions)

    # Resolve account subjects privately so blocks apply before an external delivery.
    # Outward delivery state is always submitted, including suppression and uncertain SMTP.
    if directory is None:
        from revel.auth.workos_users import WorkOSUserDirectory

        if settings.workos_api_key is None:
            raise Unavailable()
        directory = WorkOSUserDirectory(settings.workos_api_key.get_secret_value())

    def destination() -> str:
        with factory() as session:
            _start(session, principal)
            row = session.scalar(
                select(Invitation).where(
                    Invitation.id == invitation_id, Invitation.sender_user_id == principal.user_id
                )
            )
            if row is None:
                raise InvitationError()
            return row.email

    destination_email = await _run(destination)
    subjects = await directory.subjects_for_email(destination_email)
    if subjects is None:
        raise Unavailable()

    def reserve() -> InvitationInfo:
        with factory() as session, session.begin():
            _start(session, principal)
            connections._advisory_lock(
                session, connections._lock_key("revel.invites.sender", principal.user_id)
            )
            targets = list(
                session.scalars(select(User.id).where(User.workos_user_id.in_(subjects)))
            )
            for key in sorted(
                {
                    connections.pair_lock_key(a, b)
                    for target in targets
                    for a, b in ((principal.user_id, target), (target, principal.user_id))
                }
            ):
                connections._advisory_lock(session, key)
            row = session.scalar(
                select(Invitation)
                .where(
                    Invitation.id == invitation_id, Invitation.sender_user_id == principal.user_id
                )
                .with_for_update()
            )
            now = session.scalar(select(func.now()))
            if row is None or row.status != "pending" or row.expires_at <= now:
                raise InvitationError()
            if row.delivery_status != "not_sent":
                return _info(row, settings)
            count = session.scalar(
                select(func.count())
                .select_from(Invitation)
                .where(
                    Invitation.sender_user_id == principal.user_id,
                    Invitation.sent_at > now - timedelta(hours=1),
                )
            )
            if count >= SEND_LIMIT:
                raise RateLimited()
            blocked = session.scalar(
                select(ConnectionRequest.id)
                .where(
                    ConnectionRequest.status == "blocked",
                    or_(
                        (ConnectionRequest.requester_user_id == principal.user_id)
                        & ConnectionRequest.target_user_id.in_(targets),
                        ConnectionRequest.requester_user_id.in_(targets)
                        & (ConnectionRequest.target_user_id == principal.user_id),
                    ),
                )
                .limit(1)
            )
            row.sent_at = now
            if blocked or principal.user_id in targets:
                row.delivery_status = "unknown"
                return _info(row, settings)
            row.delivery_status = "sending"
            return _info(row, settings, link=True)

    reserved = await _run(reserve)
    if reserved.invite_url is None:
        return reserved
    try:
        await mailer.send_invitation(
            destination=reserved.email,
            invite_url=reserved.invite_url,
            idempotency_key=str(invitation_id),
        )
        outcome = "sent"
    except Exception:
        # A timeout may have delivered. Never automatically resend this record.
        outcome = "unknown"

    def finish() -> InvitationInfo:
        with factory() as session, session.begin():
            row = session.get(Invitation, invitation_id, with_for_update=True)
            row.delivery_status = outcome
            return _info(row, settings)

    return await _run(finish)


async def _verified_email(
    principal: Principal, factory: Any, settings: Settings, directory: Any = None
) -> str:
    from revel.auth.workos_users import WorkOSUserDirectory

    def subject() -> str:
        with factory() as session:
            _start(session, principal)
            user = session.get(User, principal.user_id)
            if user is None:
                raise EmailVerificationRequired()
            return user.workos_user_id

    workos_id = await _run(subject)
    if directory is None:
        if not settings.workos_api_key:
            raise Unavailable()
        directory = WorkOSUserDirectory(settings.workos_api_key.get_secret_value())
    email = await directory.fetch_verified_email(workos_id)
    if email is None:
        raise EmailVerificationRequired()
    return normalize_email(email)


async def redeem(
    principal: Principal,
    *,
    token: str,
    action: Literal["preview", "accept", "decline"],
    sessions: Any = None,
    settings: Settings | None = None,
    directory: Any = None,
) -> InvitationPreview | InvitationInfo:
    if principal.via != "ui" or action not in ("preview", "accept", "decline"):
        raise InvitationError()
    token_hash = _token_hash(token)
    settings = settings or get_settings()
    factory = connections._factory(sessions)
    email = await _verified_email(principal, factory, settings, directory)

    def work() -> InvitationPreview | InvitationInfo:
        with factory() as session, session.begin():
            _start(session, principal)
            candidate = session.scalar(
                select(Invitation).where(Invitation.token_hash == token_hash)
            )
            if candidate is None:
                raise InvitationError()
            sender = candidate.sender_user_id
            # Take both directed connection locks before invitation/connection row locks.
            for key in sorted(
                {
                    connections.pair_lock_key(sender, principal.user_id),
                    connections.pair_lock_key(principal.user_id, sender),
                }
            ):
                connections._advisory_lock(session, key)
            row = session.scalar(
                select(Invitation)
                .where(Invitation.id == candidate.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            now = session.scalar(select(func.now()))
            if row.status != "pending" or row.expires_at <= now:
                raise InvitationError()
            if row.email != email or sender == principal.user_id:
                raise EmailVerificationRequired()
            blocked = session.scalar(
                select(ConnectionRequest.id)
                .where(
                    ConnectionRequest.status == "blocked",
                    or_(
                        (ConnectionRequest.requester_user_id == sender)
                        & (ConnectionRequest.target_user_id == principal.user_id),
                        (ConnectionRequest.requester_user_id == principal.user_id)
                        & (ConnectionRequest.target_user_id == sender),
                    ),
                )
                .limit(1)
            )
            if blocked:
                raise InvitationError()
            if action == "preview":
                user = session.get(User, sender)
                return InvitationPreview(
                    sender_name=user.display_name, expires_at=row.expires_at, status=row.status
                )
            row.status = "accepted" if action == "accept" else "declined"
            row.responded_at = now
            row.recipient_user_id = principal.user_id
            if action == "accept":
                request = connections._live_row(session, sender, principal.user_id, for_update=True)
                if request is None:
                    request = ConnectionRequest(
                        requester_user_id=sender, target_user_id=principal.user_id
                    )
                    session.add(request)
                    session.flush()
                row.connection_request_id = request.id
            return _info(row, settings)

    return await _run(work)
