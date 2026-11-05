"""Resend transport contract with synthetic credentials and HTTP responses only."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from revel.services.invitation_mail import configured_mailer
from revel.services.invitations import DisabledMailer, MailUnavailable
from revel.services.resend_mail import (
    RESEND_EMAILS_URL,
    ResendDeliveryFailed,
    ResendInvitationMailer,
)
from revel.settings import Settings


@pytest.fixture
def config():
    return Settings(
        _env_file=None,
        public_base_url="https://revel.example",
        invitation_from_email="invites@example.test",
        resend_api_key=SecretStr("re_synthetic_secret"),
    )


def test_resend_selection_retains_disabled_and_runtime_gate(config):
    assert isinstance(configured_mailer(config), DisabledMailer)
    config.invitation_mail_provider = "resend"
    with pytest.raises(MailUnavailable):
        configured_mailer(config)
    config.invitation_background_delivery_ready = True
    assert isinstance(configured_mailer(config), ResendInvitationMailer)
    config.resend_api_key = None
    with pytest.raises(MailUnavailable):
        configured_mailer(config)


@pytest.mark.anyio
async def test_resend_contract_and_private_payload(config, caplog):
    requests = []
    provider_id, invitation_id = str(uuid4()), str(uuid4())

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": provider_id})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        result = await ResendInvitationMailer(config, http_client=client).send_invitation(
            sender_name="Leo",
            expires_at=datetime(2026, 11, 12, 18, 30, tzinfo=UTC),
            destination="Friend@Example.Test",
            invite_url="https://revel.example/app/invite#token=synthetic",
            idempotency_key=invitation_id,
        )
    assert result == provider_id
    request = requests[0]
    assert str(request.url) == RESEND_EMAILS_URL
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer re_synthetic_secret"
    assert request.headers["Idempotency-Key"] == invitation_id
    assert request.extensions["timeout"] == {"connect": 3, "read": 10, "write": 10, "pool": 10}
    payload = json.loads(request.content)
    assert payload["from"] == "Revel Gifts <invites@example.test>"
    from revel.services.invitation_email import render_invitation

    expected = render_invitation(
        sender_name="Leo",
        expires_at=datetime(2026, 11, 12, 18, 30, tzinfo=UTC),
        invite_url="https://revel.example/app/invite#token=synthetic",
    )
    assert payload["subject"] == expected.subject
    assert payload["html"] == expected.html
    assert payload["text"] == expected.text
    assert payload["to"] == ["friend@example.test"]
    assert set(payload) == {"from", "to", "subject", "text", "html"}
    assert "#token=synthetic" in payload["text"]
    assert "re_synthetic_secret" not in caplog.text
    assert "friend@example.test" not in caplog.text
    assert "#token=synthetic" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,body",
    [
        (400, {"message": "private"}),
        (429, {"message": "private"}),
        (500, {"message": "private"}),
        (200, {}),
        (200, {"id": 123}),
        (200, {"id": "bad"}),
        (200, []),
        (302, {"id": str(uuid4())}),
    ],
)
async def test_resend_errors_are_generic_and_not_retried(config, status, body):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json=body, headers={"Location": "https://untrusted.example/"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        with pytest.raises(ResendDeliveryFailed) as error:
            await ResendInvitationMailer(config, http_client=client).send_invitation(
                sender_name="Leo",
                expires_at=datetime(2026, 11, 12, 18, 30, tzinfo=UTC),
                destination="friend@example.test",
                invite_url="https://revel.example/app/invite#token=private",
                idempotency_key=str(uuid4()),
            )
    assert len(requests) == 1
    assert "private" not in str(error.value)
    assert "re_synthetic_secret" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["timeout", "invalid_json"])
async def test_resend_timeout_and_invalid_json_are_unknown_without_retry(config, kind):
    requests = []

    def handle(request):
        requests.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("private token and destination", request=request)
        return httpx.Response(200, text="private invalid json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ResendDeliveryFailed) as error:
            await ResendInvitationMailer(config, http_client=client).send_invitation(
                sender_name="Leo",
                expires_at=datetime(2026, 11, 12, 18, 30, tzinfo=UTC),
                destination="friend@example.test",
                invite_url="https://revel.example/app/invite#token=private",
                idempotency_key=str(uuid4()),
            )
    assert len(requests) == 1
    assert "private" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "http://revel.example/app/invite#token=x",
        "https://evil.example/app/invite#token=x",
        "https://revel.example/app/invite?token=x",
        "https://revel.example/app/other#token=x",
    ],
)
async def test_resend_never_transmits_invalid_capability_url(config, url):
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as client:
        with pytest.raises(MailUnavailable):
            await ResendInvitationMailer(config, http_client=client).send_invitation(
                sender_name="Leo",
                expires_at=datetime(2026, 11, 12, 18, 30, tzinfo=UTC),
                destination="friend@example.test",
                invite_url=url,
                idempotency_key=str(uuid4()),
            )
    assert not requests


@pytest.mark.anyio
async def test_resend_hard_deadline_cancels_a_stalled_response(config, monkeypatch):
    import anyio

    from revel.services import resend_mail

    deadlines, requests = [], []
    original = anyio.fail_after

    def fast_deadline(seconds):
        deadlines.append(seconds)
        return original(0.01)

    async def stalls(request):
        requests.append(request)
        await anyio.sleep_forever()

    monkeypatch.setattr(resend_mail.anyio, "fail_after", fast_deadline)
    async with httpx.AsyncClient(transport=httpx.MockTransport(stalls)) as client:
        with pytest.raises(ResendDeliveryFailed):
            await ResendInvitationMailer(config, http_client=client).send_invitation(
                sender_name="Leo",
                expires_at=datetime(2026, 11, 12, 18, 30, tzinfo=UTC),
                destination="friend@example.test",
                invite_url="https://revel.example/app/invite#token=x",
                idempotency_key=str(uuid4()),
            )
    assert deadlines == [15]
    assert len(requests) == 1
