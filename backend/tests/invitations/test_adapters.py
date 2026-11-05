"""Invitation transport guards and disabled-by-default mail configuration."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import SecretStr

from revel.api.invitations import router
from revel.auth.identity import Principal
from revel.auth.ui import current_ui_principal
from revel.mcp import invitation_tools
from revel.services import invitations as svc
from revel.services.invitation_mail import SMTPInvitationMailer, configured_mailer
from revel.settings import Settings


@pytest.fixture
def config():
    return Settings(
        _env_file=None,
        app_env="test",
        public_base_url="https://revel.example",
        workos_cookie_password=SecretStr("s" * 40),
        workos_api_key=SecretStr("test-key"),
        workos_client_id="client_test",
        workos_authkit_domain="test.authkit.app",
    )


@pytest.fixture
def app(config):
    app = FastAPI()
    app.state.settings = config
    app.include_router(router)
    return app


def test_anonymous_invitation_routes_require_signin(app):
    with TestClient(app, base_url="https://revel.example") as client:
        assert client.get("/api/invitations").status_code == 401
        assert (
            client.post(
                "/api/invitations/preview",
                json={"token": "a" * 43},
                headers={"Origin": "https://revel.example"},
            ).status_code
            == 401
        )


def test_mutations_check_csrf_and_success_is_not_cached(app, monkeypatch):
    principal = Principal(uuid4(), None, None, "ui")
    app.dependency_overrides[current_ui_principal] = lambda: principal
    calls = []

    async def create(actor, **kwargs):
        calls.append((actor, kwargs))
        return {"id": str(uuid4()), "email": kwargs["email"]}

    monkeypatch.setattr(svc, "create", create)
    with TestClient(app, base_url="https://revel.example") as client:
        for headers in [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}]:
            assert (
                client.post(
                    "/api/invitations", json={"email": "a@example.test"}, headers=headers
                ).status_code
                == 403
            )
        result = client.post(
            "/api/invitations",
            json={"email": "a@example.test"},
            headers={"Origin": "https://revel.example"},
        )
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["referrer-policy"] == "no-referrer"
        assert calls[0][0] == principal
        assert len(calls) == 1


@pytest.mark.anyio
async def test_mcp_always_resolves_principal_before_service(config, monkeypatch):
    server = MCPServer(name="invitation-test")
    invitation_tools.register(server, config)
    calls = []

    async def deny(runtime):
        calls.append("authenticate")
        raise ToolError("Authentication required")

    monkeypatch.setattr(invitation_tools, "principal_from_mcp_context", deny)
    with pytest.raises(ToolError, match="Authentication required"):
        await server.call_tool("manage_invitations", {"action": "list"})
    assert calls == ["authenticate"]


def test_mail_defaults_disabled_and_requires_full_configuration(config):
    assert isinstance(configured_mailer(config), svc.DisabledMailer)
    config.invitation_mail_provider = "smtp"
    with pytest.raises(svc.MailUnavailable):
        configured_mailer(config)


def test_smtp_uses_verified_tls_and_explicit_envelope(config, monkeypatch):
    config.invitation_mail_provider = "smtp"
    config.invitation_smtp_host = "smtp.example.test"
    config.invitation_smtp_username = "test-user"
    config.invitation_smtp_password = SecretStr("test-password")
    config.invitation_from_email = "invites@example.test"
    calls = []

    class SMTP:
        def __init__(self, host, port, *, timeout, context):
            import ssl

            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname is True
            calls.append((host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, username, password):
            calls.append((username, password))

        def send_message(self, message, *, from_addr, to_addrs):
            calls.append((message, from_addr, to_addrs))

    monkeypatch.setattr("revel.services.invitation_mail.smtplib.SMTP_SSL", SMTP)
    mailer = SMTPInvitationMailer(config)
    mailer._send(
        "friend@example.test",
        "https://revel.example/app/invite#token=synthetic",
        "test-id",
        sender_name="Leo",
        expires_at=datetime(2026, 11, 12, tzinfo=UTC),
    )
    assert calls[0] == ("smtp.example.test", 465, 10)
    message, sender, destinations = calls[-1]
    from revel.services.invitation_email import render_invitation

    expected = render_invitation(
        sender_name="Leo",
        expires_at=datetime(2026, 11, 12, tzinfo=UTC),
        invite_url="https://revel.example/app/invite#token=synthetic",
    )
    assert str(message["From"]) == "Revel Gifts <invites@example.test>"
    assert str(message["Subject"]) == expected.subject
    assert (
        message.get_body(preferencelist=("plain",)).get_content().rstrip() == expected.text.rstrip()
    )
    assert (
        message.get_body(preferencelist=("html",)).get_content().rstrip() == expected.html.rstrip()
    )
    assert sender == "invites@example.test"
    assert destinations == ["friend@example.test"]
    assert "#token=synthetic" in message.get_body(preferencelist=("plain",)).get_content()
    with pytest.raises(svc.MailUnavailable):
        mailer._send(
            "friend@example.test",
            "https://evil.example/app/invite#token=x",
            "test-id",
            sender_name="Leo",
            expires_at=datetime(2026, 11, 12, tzinfo=UTC),
        )
    with pytest.raises(svc.InvalidEmail):
        mailer._send(
            "friend@example.test\r\nBcc: hidden@example.test",
            "https://revel.example/",
            "test-id",
            sender_name="Leo",
            expires_at=datetime(2026, 11, 12, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "email",
    [
        "evil<victim@example.test>",
        "a:b@example.test",
        "a(comment)@example.test",
        "Display Name <victim@example.test>",
        "a@example.test,b@example.test",
        '"quoted"@example.test',
        "a..b@example.test",
        ".a@example.test",
        "a.@example.test",
        "a@-example.test",
        "a@example-.test",
        "a@ex ample.test",
        "a\r\nb@example.test",
    ],
)
def test_mailbox_cannot_be_reinterpreted_by_smtp(email):
    with pytest.raises(svc.InvalidEmail):
        svc.normalize_email(email)


@pytest.mark.parametrize("email", ["person+tag@example.test", "first.last@sub.example.test"])
def test_supported_mailbox_has_exact_smtp_envelope(email):
    from smtplib import quoteaddr

    canonical = svc.normalize_email(email)
    assert quoteaddr(canonical) == f"<{canonical}>"


def test_application_owns_and_releases_mail_dispatcher(config):
    from revel.app import create_app
    from revel.services.invitation_mail import dispatcher_for

    assert dispatcher_for(config) is None
    with TestClient(create_app(config)):
        assert dispatcher_for(config).running
    assert dispatcher_for(config) is None


@pytest.mark.anyio
async def test_dispatcher_observes_worker_errors_and_drains():
    from revel.services.invitation_mail import MailDispatcher

    dispatcher = MailDispatcher()
    calls = []

    async def fails():
        calls.append(True)
        raise RuntimeError("synthetic worker failure")

    async with dispatcher.run():
        dispatcher.enqueue(fails)
        await dispatcher.wait_idle()
    assert calls == [True]
    assert not dispatcher.running


def test_smtp_requires_explicit_background_runtime_gate(config):
    config.invitation_mail_provider = "smtp"
    config.invitation_smtp_host = "smtp.example.test"
    config.invitation_smtp_username = "test-user"
    config.invitation_smtp_password = SecretStr("test-password")
    config.invitation_from_email = "invites@example.test"
    with pytest.raises(svc.MailUnavailable):
        configured_mailer(config)
    config.invitation_background_delivery_ready = True
    assert isinstance(configured_mailer(config), SMTPInvitationMailer)
