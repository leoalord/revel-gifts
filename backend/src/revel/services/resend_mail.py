"""Native Resend invitation adapter. Fixed HTTPS origin, no redirects, no automatic retry."""

from uuid import UUID

import anyio
import httpx

from revel.services.invitation_email import branded_sender, render_invitation
from revel.services.invitations import MailUnavailable, normalize_email
from revel.settings import Settings

RESEND_EMAILS_URL = "https://api.resend.com/emails"


class ResendDeliveryFailed(Exception):
    """Safe generic error; never propagate provider bodies, headers or private payloads."""

    def __init__(self):
        super().__init__("Invitation provider did not confirm acceptance")


class ResendInvitationMailer:
    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None):
        if settings.resend_api_key is None or not settings.resend_api_key.get_secret_value():
            raise MailUnavailable()
        if not settings.invitation_from_email:
            raise MailUnavailable()
        self._api_key = settings.resend_api_key
        self._sender = normalize_email(settings.invitation_from_email)
        self._origin = settings.public_origin
        self._client = http_client

    async def send_invitation(
        self,
        *,
        destination: str,
        invite_url: str,
        idempotency_key: str,
        sender_name: str | None,
    ) -> str:
        from urllib.parse import urlsplit

        destination = normalize_email(destination)
        parts = urlsplit(invite_url)
        if (
            parts.scheme != "https"
            or f"{parts.scheme}://{parts.netloc}" != self._origin
            or parts.path != "/app/invite"
            or parts.query
            or not parts.fragment.startswith("token=")
        ):
            raise MailUnavailable()
        try:
            key = str(UUID(idempotency_key))
        except (ValueError, TypeError, AttributeError):
            raise ResendDeliveryFailed() from None
        headers = {
            "Authorization": "Bearer " + self._api_key.get_secret_value(),
            "Idempotency-Key": key,
            "Accept": "application/json",
        }
        content = render_invitation(sender_name=sender_name, invite_url=invite_url)
        body = {
            "from": branded_sender(self._sender),
            "to": [destination],
            "subject": content.subject,
            "text": content.text,
            "html": content.html,
        }
        try:
            # Explicit redirect/timeout arguments also override an injected client's defaults.
            with anyio.fail_after(15):
                if self._client is not None:
                    response = await self._client.post(
                        RESEND_EMAILS_URL,
                        json=body,
                        headers=headers,
                        follow_redirects=False,
                        timeout=httpx.Timeout(10, connect=3),
                    )
                else:
                    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                        response = await client.post(
                            RESEND_EMAILS_URL,
                            json=body,
                            headers=headers,
                            follow_redirects=False,
                            timeout=httpx.Timeout(10, connect=3),
                        )
            if response.status_code not in (200, 201):
                raise ResendDeliveryFailed()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("id"), str):
                raise ResendDeliveryFailed()
            provider_id = str(UUID(data["id"]))
            if provider_id != data["id"]:
                raise ResendDeliveryFailed()
            return provider_id
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
            raise ResendDeliveryFailed() from None
