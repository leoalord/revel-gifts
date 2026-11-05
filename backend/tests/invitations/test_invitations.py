"""Security and lifecycle checks against disposable PostgreSQL, never hosted state."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker

from revel.auth.identity import Principal
from revel.auth.workos_users import WorkOSUserDirectory
from revel.db.models import Agent, AgentDelegation, ConnectionRequest, Invitation, User
from revel.services import invitations as svc
from revel.settings import Settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def config():
    return Settings(
        _env_file=None,
        public_base_url="https://revel.example",
        workos_cookie_password=SecretStr("s" * 40),
    )


@pytest.fixture(autouse=True)
async def mail_dispatcher(config):
    from revel.services.invitation_mail import dispatcher_lifespan

    async with dispatcher_lifespan(config) as dispatcher:
        yield dispatcher


@pytest.fixture
def factory():
    url = os.environ.get("REVEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("REVEL_TEST_DATABASE_URL missing; PostgreSQL acceptance cannot run")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users, agents CASCADE"))
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        # Other suites rebuild this disposable schema. Remove this fixture's synthetic
        # nonexpiring invites before their guarded downgrade; production guard stays intact.
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE users, agents CASCADE"))
        engine.dispose()


@pytest.fixture
def people(factory):
    with factory() as session, session.begin():
        sender = User(
            id=uuid4(),
            workos_user_id="user_sender",
            email="stale@example.test",
            display_name="Sender",
        )
        recipient = User(id=uuid4(), workos_user_id="user_recipient", email="wrong@example.test")
        other = User(id=uuid4(), workos_user_id="user_other")
        session.add_all([sender, recipient, other])
    return tuple(
        Principal(person.id, None, None, via="ui") for person in (sender, recipient, other)
    )


class Directory:
    def __init__(self, email="friend@example.test"):
        self.email = email
        self.subjects = []

    async def subjects_for_email(self, email):
        return []

    async def fetch_verified_email(self, subject):
        self.subjects.append(subject)
        return self.email


class Mailer:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_invitation(self, **kwargs):
        self.sent.append(kwargs)
        if self.fail:
            raise TimeoutError()


def token(info):
    assert "?" not in info.invite_url
    return info.invite_url.split("#token=")[1]


async def make(people, factory, config, **kwargs):
    return await svc.create(
        people[0], email="Friend@Example.Test", settings=config, sessions=lambda: factory, **kwargs
    )


@pytest.mark.anyio
async def test_accept_is_single_use_and_never_grants_access(people, factory, config):
    info = await make(people, factory, config)
    directory = Directory()
    preview = await svc.redeem(
        people[1],
        token=token(info),
        action="preview",
        settings=config,
        sessions=lambda: factory,
        directory=directory,
    )
    assert preview.sender_name == "Sender"
    assert directory.subjects == ["user_recipient"]
    accepted = await svc.redeem(
        people[1],
        token=token(info),
        action="accept",
        settings=config,
        sessions=lambda: factory,
        directory=directory,
    )
    assert accepted.status == "accepted"
    with factory() as session:
        request = session.get(ConnectionRequest, accepted.connection_request_id)
        assert request.requester_user_id == people[0].user_id
        assert request.target_user_id == people[1].user_id
        assert request.status == "pending"
        assert request.granted_tier is None
    with pytest.raises(svc.InvitationError):
        await svc.redeem(
            people[1],
            token=token(info),
            action="accept",
            settings=config,
            sessions=lambda: factory,
            directory=directory,
        )


@pytest.mark.anyio
@pytest.mark.parametrize("verified_email", [None, "other@example.test"])
async def test_wrong_or_unverified_email_does_not_consume(people, factory, config, verified_email):
    info = await make(people, factory, config)
    with pytest.raises(svc.EmailVerificationRequired):
        await svc.redeem(
            people[1],
            token=token(info),
            action="accept",
            settings=config,
            sessions=lambda: factory,
            directory=Directory(verified_email),
        )
    with factory() as session:
        assert session.get(Invitation, info.id).status == "pending"
        assert session.scalar(select(ConnectionRequest.id)) is None


@pytest.mark.anyio
async def test_cancel_authorization_and_nonexpiring_legacy_pending(people, factory, config):
    info = await make(people, factory, config)
    with pytest.raises(svc.InvitationError):
        await svc.cancel(people[2], info.id, sessions=lambda: factory, settings=config)
    cancelled = await svc.cancel(people[0], info.id, sessions=lambda: factory, settings=config)
    assert cancelled.status == "cancelled"
    with pytest.raises(svc.InvitationError):
        await svc.redeem(
            people[1],
            token=token(info),
            action="accept",
            settings=config,
            sessions=lambda: factory,
            directory=Directory(),
        )
    info = await make(people, factory, config)
    with factory() as session, session.begin():
        session.execute(
            update(Invitation)
            .where(Invitation.id == info.id)
            .values(
                created_at=datetime.now(UTC) - timedelta(days=8),
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
    joined = await svc.join(
        people[1],
        token=token(info),
        settings=config,
        sessions=lambda: factory,
        directory=Directory(),
    )
    assert joined.status == "joined" and joined.expires_at is None


@pytest.mark.anyio
@pytest.mark.parametrize("reverse", [False, True])
async def test_blocks_prevent_redemption(people, factory, config, reverse):
    info = await make(people, factory, config)
    a, b = people[:2]
    if reverse:
        a, b = b, a
    with factory() as session, session.begin():
        session.add(
            ConnectionRequest(
                requester_user_id=a.user_id,
                target_user_id=b.user_id,
                status="blocked",
                responded_at=datetime.now(UTC),
            )
        )
    with pytest.raises(svc.InvitationError):
        await svc.redeem(
            people[1],
            token=token(info),
            action="accept",
            settings=config,
            sessions=lambda: factory,
            directory=Directory(),
        )


@pytest.mark.anyio
async def test_decline_creates_no_connection(people, factory, config):
    info = await make(people, factory, config)
    result = await svc.redeem(
        people[1],
        token=token(info),
        action="decline",
        settings=config,
        sessions=lambda: factory,
        directory=Directory(),
    )
    assert result.status == "declined"
    with factory() as session:
        assert session.scalar(select(ConnectionRequest.id)) is None


@pytest.mark.anyio
async def test_create_dedupe_and_send_confirmation(people, factory, config):
    info = await make(people, factory, config)
    repeated = await make(people, factory, config)
    assert repeated.id == info.id
    assert repeated.invite_url == info.invite_url
    mailer = Mailer()
    with pytest.raises(svc.ConfirmationRequired):
        await svc.send(
            people[0],
            info.id,
            mailer=mailer,
            directory=Directory(),
            settings=config,
            sessions=lambda: factory,
        )
    sent = await svc.send(
        people[0],
        info.id,
        owner_confirmed=True,
        mailer=mailer,
        directory=Directory(),
        settings=config,
        sessions=lambda: factory,
    )
    assert sent.delivery_status == "submitted"
    await svc.send(
        people[0],
        info.id,
        owner_confirmed=True,
        mailer=mailer,
        directory=Directory(),
        settings=config,
        sessions=lambda: factory,
    )
    from revel.services.invitation_mail import dispatcher_for

    await dispatcher_for(config).wait_idle()
    assert len(mailer.sent) == 1
    assert mailer.sent[0]["destination"] == "friend@example.test"
    with factory() as session:
        row = session.get(Invitation, info.id)
        assert token(info) not in row.sealed_token
        assert token(info) not in row.token_hash


@pytest.mark.anyio
async def test_unknown_mail_outcome_never_resends(people, factory, config):
    info = await make(people, factory, config)
    mailer = Mailer(fail=True)
    result = await svc.send(
        people[0],
        info.id,
        owner_confirmed=True,
        mailer=mailer,
        directory=Directory(),
        settings=config,
        sessions=lambda: factory,
    )
    assert result.delivery_status == "submitted"
    await svc.send(
        people[0],
        info.id,
        owner_confirmed=True,
        mailer=mailer,
        directory=Directory(),
        settings=config,
        sessions=lambda: factory,
    )
    from revel.services.invitation_mail import dispatcher_for

    await dispatcher_for(config).wait_idle()
    assert len(mailer.sent) == 1


@pytest.mark.anyio
async def test_creation_limit_and_inactive_delegation(people, factory, config, monkeypatch):
    monkeypatch.setattr(svc, "CREATE_LIMIT", 1)
    await make(people, factory, config)
    with pytest.raises(svc.RateLimited):
        await svc.create(
            people[0], email="second@example.test", settings=config, sessions=lambda: factory
        )
    with factory() as session, session.begin():
        agent = Agent(id=uuid4(), oauth_client_id="test_client")
        session.add(agent)
        session.flush()
        delegation = AgentDelegation(
            id=uuid4(),
            agent_id=agent.id,
            user_id=people[0].user_id,
            status="disconnected",
            disconnected_at=datetime.now(UTC),
        )
        session.add(delegation)
    actor = Principal(people[0].user_id, agent.id, delegation.id, "mcp")
    with pytest.raises(svc.Unavailable):
        await svc.list_sent(actor, settings=config, sessions=lambda: factory)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        {"id": "user_1", "email": "verified@example.test", "email_verified": True},
        {"id": "user_1", "email": "unverified@example.test", "email_verified": False},
        {"id": "user_1", "email": "missing@example.test"},
        {"id": "wrong_subject", "email": "verified@example.test", "email_verified": True},
    ],
)
async def test_directory_requires_fresh_verified_matching_subject(response):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        directory = WorkOSUserDirectory("test-key", http_client=client)
        email = await directory.fetch_verified_email("user_1")
        expected = (
            "verified@example.test"
            if response.get("email_verified") is True and response["id"] == "user_1"
            else None
        )
        assert email == expected


@pytest.mark.anyio
async def test_concurrent_create_and_accept_are_atomic(people, factory, config):
    import asyncio

    results = await asyncio.gather(*(make(people, factory, config) for _ in range(4)))
    assert len({info.id for info in results}) == 1
    info = results[0]
    responses = await asyncio.gather(
        *(
            svc.redeem(
                people[1],
                token=token(info),
                action="accept",
                settings=config,
                sessions=lambda: factory,
                directory=Directory(),
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, svc.InvitationInfo) for result in responses) == 1
    assert sum(isinstance(result, svc.InvitationError) for result in responses) == 1
    with factory() as session:
        assert len(list(session.scalars(select(ConnectionRequest)))) == 1


@pytest.mark.anyio
async def test_concurrent_send_reserves_once(people, factory, config):
    import asyncio

    info = await make(people, factory, config)
    mailer = Mailer()
    await asyncio.gather(
        *(
            svc.send(
                people[0],
                info.id,
                owner_confirmed=True,
                mailer=mailer,
                directory=Directory(),
                settings=config,
                sessions=lambda: factory,
            )
            for _ in range(4)
        )
    )
    from revel.services.invitation_mail import dispatcher_for

    await dispatcher_for(config).wait_idle()
    assert len(mailer.sent) == 1


@pytest.mark.anyio
async def test_disabled_mail_does_not_prevent_copy(people, factory, config):
    info = await make(people, factory, config)
    with pytest.raises(svc.MailUnavailable):
        await svc.send(
            people[0], info.id, owner_confirmed=True, settings=config, sessions=lambda: factory
        )
    assert token(info)
    assert (await svc.list_sent(people[0], sessions=lambda: factory, settings=config))[
        0
    ].delivery_status == "not_sent"


@pytest.mark.anyio
async def test_delivery_block_is_private_and_prevents_external_send(people, factory, config):
    info = await make(people, factory, config)
    with factory() as session, session.begin():
        session.add(
            ConnectionRequest(
                requester_user_id=people[0].user_id,
                target_user_id=people[1].user_id,
                status="blocked",
                responded_at=datetime.now(UTC),
            )
        )

    class BlockedDirectory:
        async def subjects_for_email(self, email):
            return ["user_recipient"]

    mailer = Mailer()
    result = await svc.send(
        people[0],
        info.id,
        owner_confirmed=True,
        mailer=mailer,
        directory=BlockedDirectory(),
        settings=config,
        sessions=lambda: factory,
    )
    assert result.delivery_status == "submitted"
    from revel.services.invitation_mail import dispatcher_for

    await dispatcher_for(config).wait_idle()
    assert not mailer.sent
    with factory() as session:
        assert session.get(Invitation, info.id).delivery_status == "unknown"


@pytest.mark.anyio
@pytest.mark.parametrize("existing_status,tier", [("pending", None), ("approved", "connections")])
async def test_accept_reuses_existing_directional_request(
    people, factory, config, existing_status, tier
):
    info = await make(people, factory, config)
    with factory() as session, session.begin():
        request = ConnectionRequest(
            requester_user_id=people[0].user_id,
            target_user_id=people[1].user_id,
            status=existing_status,
            granted_tier=tier,
            responded_at=datetime.now(UTC) if tier else None,
        )
        session.add(request)
        session.flush()
        request_id = request.id
    result = await svc.redeem(
        people[1],
        token=token(info),
        action="accept",
        settings=config,
        sessions=lambda: factory,
        directory=Directory(),
    )
    assert result.connection_request_id == request_id
    with factory() as session:
        assert len(list(session.scalars(select(ConnectionRequest)))) == 1
        assert session.get(ConnectionRequest, request_id).granted_tier == tier


@pytest.mark.anyio
async def test_send_rate_limit_is_exact(people, factory, config, monkeypatch):
    monkeypatch.setattr(svc, "SEND_LIMIT", 1)
    info = await make(people, factory, config)
    mailer = Mailer()
    await svc.send(
        people[0],
        info.id,
        owner_confirmed=True,
        mailer=mailer,
        directory=Directory(),
        settings=config,
        sessions=lambda: factory,
    )
    other = await svc.create(
        people[0], email="second@example.test", sessions=lambda: factory, settings=config
    )
    with pytest.raises(svc.RateLimited):
        await svc.send(
            people[0],
            other.id,
            owner_confirmed=True,
            mailer=mailer,
            directory=Directory(),
            settings=config,
            sessions=lambda: factory,
        )
    from revel.services.invitation_mail import dispatcher_for

    await dispatcher_for(config).wait_idle()
    assert len(mailer.sent) == 1


@pytest.mark.anyio
async def test_concurrent_accept_cancel_one_wins(people, factory, config):
    import asyncio

    info = await make(people, factory, config)
    results = await asyncio.gather(
        svc.redeem(
            people[1],
            token=token(info),
            action="accept",
            settings=config,
            sessions=lambda: factory,
            directory=Directory(),
        ),
        svc.cancel(people[0], info.id, sessions=lambda: factory, settings=config),
        return_exceptions=True,
    )
    assert sum(isinstance(result, svc.InvitationInfo) for result in results) == 1
    with factory() as session:
        row = session.get(Invitation, info.id)
        requests = list(session.scalars(select(ConnectionRequest)))
        assert (row.status == "accepted" and len(requests) == 1) or (
            row.status == "cancelled" and not requests
        )


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["http", "mcp"])
@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("fail_transport", [False, True])
async def test_send_response_never_waits_for_private_lookup_or_transport(
    people, factory, config, monkeypatch, transport, blocked, fail_transport, mail_dispatcher
):
    import anyio
    from fastapi import FastAPI
    from mcp.server.mcpserver import MCPServer

    from revel.api.invitations import router
    from revel.auth.ui import current_ui_principal
    from revel.mcp import invitation_tools
    from revel.services import connections, invitation_mail

    config.workos_api_key = SecretStr("test-key")
    config.workos_client_id = "client_test"
    config.workos_authkit_domain = "test.authkit.app"
    info = await make(people, factory, config)
    if blocked:
        with factory() as session, session.begin():
            session.add(
                ConnectionRequest(
                    requester_user_id=people[0].user_id,
                    target_user_id=people[1].user_id,
                    status="blocked",
                    responded_at=datetime.now(UTC),
                )
            )

    lookup_started, lookup_release = anyio.Event(), anyio.Event()
    mail_started, mail_release = anyio.Event(), anyio.Event()
    calls = []

    class GatedDirectory:
        async def subjects_for_email(self, email):
            lookup_started.set()
            await lookup_release.wait()
            return ["user_recipient"]

    class GatedMailer:
        async def send_invitation(self, **kwargs):
            calls.append(kwargs)
            mail_started.set()
            await mail_release.wait()
            if fail_transport:
                raise TimeoutError("synthetic private failure")

    monkeypatch.setattr(invitation_mail, "configured_mailer", lambda _: GatedMailer())
    monkeypatch.setattr("revel.auth.workos_users.WorkOSUserDirectory", lambda _: GatedDirectory())
    monkeypatch.setattr(connections, "_default_sessionmaker", lambda: factory)
    principal_calls = []

    async def authenticated(_):
        principal_calls.append(True)
        return people[0]

    if transport == "http":
        app = FastAPI()
        app.state.settings = config
        app.include_router(router)
        app.dependency_overrides[current_ui_principal] = lambda: people[0]

        async def request_send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://revel.example"
            ) as client:
                result = await client.post(
                    f"/api/invitations/{info.id}/send",
                    json={"owner_confirmed": True},
                    headers={"Origin": "https://revel.example"},
                )
            assert result.status_code == 200
            return result.json()
    else:
        server = MCPServer(name="send-timing-regression")
        invitation_tools.register(server, config)
        monkeypatch.setattr(invitation_tools, "principal_from_mcp_context", authenticated)

        async def request_send():
            result = await server.call_tool(
                "manage_invitations",
                {"action": "send", "invitation_id": str(info.id), "owner_confirmed": True},
            )
            assert not result.is_error
            import json

            return result.structured_content or json.loads(result.content[0].text)

    try:
        # A gated directory cannot finish. A public response that awaits it fails this check.
        with anyio.fail_after(1):
            response = await request_send()
        assert response["delivery_status"] == "submitted"
        assert response["invite_url"] is None
        with anyio.fail_after(1):
            await lookup_started.wait()
        assert not lookup_release.is_set()
        lookup_release.set()
        if not blocked:
            with anyio.fail_after(1):
                await mail_started.wait()
            assert not mail_release.is_set()
        mail_release.set()
        with anyio.fail_after(1):
            await mail_dispatcher.wait_idle()
        # Provider errors and suppression have the same response as a completed transport.
        repeated = await request_send()
        assert repeated == response
        assert len(calls) == (0 if blocked else 1)
        if transport == "mcp":
            assert len(principal_calls) == 2
        with factory() as session:
            expected = "unknown" if blocked or fail_transport else "sent"
            assert session.get(Invitation, info.id).delivery_status == expected
    finally:
        lookup_release.set()
        mail_release.set()


@pytest.mark.anyio
@pytest.mark.parametrize("response_kind", ["success", "error", "timeout", "invalid_id"])
async def test_resend_provider_reference_stays_private_and_unknown_never_retries(
    people, factory, config, mail_dispatcher, response_kind
):
    from revel.services.resend_mail import ResendInvitationMailer

    config.invitation_from_email = "invites@example.test"
    config.resend_api_key = SecretStr("re_synthetic_private")
    info = await make(people, factory, config)
    provider_id = str(uuid4())
    requests = []

    def handle(request):
        requests.append(request)
        if response_kind == "timeout":
            raise httpx.ReadTimeout("private synthetic failure", request=request)
        if response_kind == "error":
            return httpx.Response(429, json={"message": "private provider response"})
        return httpx.Response(
            200, json={"id": provider_id if response_kind == "success" else "bad"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        mailer = ResendInvitationMailer(config, http_client=client)
        result = await svc.send(
            people[0],
            info.id,
            owner_confirmed=True,
            settings=config,
            sessions=lambda: factory,
            directory=Directory(),
            mailer=mailer,
        )
        assert result.delivery_status == "submitted"
        assert "provider_message_id" not in result.model_dump()
        await mail_dispatcher.wait_idle()
        listed = await svc.list_sent(people[0], settings=config, sessions=lambda: factory)
        assert listed[0].delivery_status == "submitted"
        assert provider_id not in listed[0].model_dump_json()
        await svc.send(
            people[0],
            info.id,
            owner_confirmed=True,
            settings=config,
            sessions=lambda: factory,
            directory=Directory(),
            mailer=mailer,
        )
        await mail_dispatcher.wait_idle()
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == str(info.id)
    with factory() as session:
        row = session.get(Invitation, info.id)
        assert row.provider_message_id == (provider_id if response_kind == "success" else None)
        assert row.delivery_status == ("sent" if response_kind == "success" else "unknown")


@pytest.mark.anyio
async def test_mail_uses_fresh_authenticated_inviter_without_expiry(
    people, factory, config, mail_dispatcher
):
    info = await make(people, factory, config)
    with factory() as session, session.begin():
        session.get(User, people[0].user_id).display_name = "Current authenticated inviter"
        session.get(User, people[1].user_id).display_name = "Never infer this recipient name"
    mailer = Mailer()
    await svc.send(
        people[0],
        info.id,
        owner_confirmed=True,
        settings=config,
        sessions=lambda: factory,
        directory=Directory(),
        mailer=mailer,
    )
    await mail_dispatcher.wait_idle()
    assert len(mailer.sent) == 1
    assert mailer.sent[0]["sender_name"] == "Current authenticated inviter"
    assert "expires_at" not in mailer.sent[0]
    assert "recipient_name" not in mailer.sent[0]


@pytest.mark.anyio
async def test_join_is_idempotent_concurrent_attribution_without_connections_or_profiles(
    people, factory, config
):
    import asyncio

    from revel.db.models import ContributionPolicy, Profile

    info = await make(people, factory, config)
    assert info.expires_at is None
    with factory() as session, session.begin():
        session.get(Invitation, info.id).created_at = datetime(2000, 1, 1, tzinfo=UTC)
    preview = await svc.redeem(
        people[1],
        token=token(info),
        action="preview",
        settings=config,
        sessions=lambda: factory,
        directory=Directory(),
    )
    assert preview.status == "pending" and preview.expires_at is None
    with factory() as session:
        assert session.get(Invitation, info.id).responded_at is None
    results = await asyncio.gather(
        *(
            svc.join(
                people[1],
                token=token(info),
                settings=config,
                sessions=lambda: factory,
                directory=Directory(),
            )
            for _ in range(4)
        )
    )
    assert all(result.status == "joined" and result.expires_at is None for result in results)
    assert all(result.connection_request_id is None for result in results)
    with factory() as session:
        row = session.get(Invitation, info.id)
        original_time = row.responded_at
        assert row.recipient_user_id == people[1].user_id
        assert session.scalar(select(ConnectionRequest.id)) is None
        assert session.scalar(select(Profile.id)) is None
        assert session.scalar(select(ContributionPolicy.profile_id)) is None
    again = await svc.join(
        people[1],
        token=token(info),
        settings=config,
        sessions=lambda: factory,
        directory=Directory(),
    )
    joined_preview = await svc.redeem(
        people[1],
        token=token(info),
        action="preview",
        settings=config,
        sessions=lambda: factory,
        directory=Directory(),
    )
    assert again.status == joined_preview.status == "joined"
    with factory() as session:
        assert session.get(Invitation, info.id).responded_at == original_time
    with pytest.raises(svc.InvitationError):
        await svc.join(
            people[2],
            token=token(info),
            settings=config,
            sessions=lambda: factory,
            directory=Directory(),
        )
    with pytest.raises(svc.InvitationError):
        await svc.redeem(
            people[1],
            token=token(info),
            action="accept",
            settings=config,
            sessions=lambda: factory,
            directory=Directory(),
        )


@pytest.mark.anyio
@pytest.mark.parametrize("email", [None, "wrong@example.test"])
async def test_join_requires_fresh_verified_invited_email(people, factory, config, email):
    info = await make(people, factory, config)
    directory = Directory(email)
    with pytest.raises(svc.EmailVerificationRequired):
        await svc.join(
            people[1],
            token=token(info),
            settings=config,
            sessions=lambda: factory,
            directory=directory,
        )
    assert directory.subjects == ["user_recipient"]
    with factory() as session:
        row = session.get(Invitation, info.id)
        assert row.status == "pending" and row.recipient_user_id is None
        assert row.responded_at is None
        assert session.scalar(select(ConnectionRequest.id)) is None


@pytest.mark.anyio
@pytest.mark.parametrize("blocked", ["forward", "reverse", "cancelled"])
async def test_join_enforces_both_block_directions_and_cancellation(
    people, factory, config, blocked
):
    info = await make(people, factory, config)
    if blocked == "cancelled":
        await svc.cancel(people[0], info.id, settings=config, sessions=lambda: factory)
    else:
        a, b = people[:2]
        if blocked == "reverse":
            a, b = b, a
        with factory() as session, session.begin():
            session.add(
                ConnectionRequest(
                    requester_user_id=a.user_id,
                    target_user_id=b.user_id,
                    status="blocked",
                    responded_at=datetime.now(UTC),
                )
            )
    with pytest.raises(svc.InvitationError):
        await svc.join(
            people[1],
            token=token(info),
            settings=config,
            sessions=lambda: factory,
            directory=Directory(),
        )
    with factory() as session:
        assert session.get(Invitation, info.id).recipient_user_id is None
