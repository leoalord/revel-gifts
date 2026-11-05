"""Canonical, asset-free invitation content shared by HTTPS and SMTP delivery.

Visual source: Brand & UI Style Guide
https://app.notion.com/p/40f2bade7f31819b988af36e5e02b78b
"""

import unicodedata
from dataclasses import dataclass
from email.headerregistry import Address
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import urlsplit


@dataclass(frozen=True)
class InvitationEmail:
    subject: str
    text: str
    html: str


def safe_sender_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    # Header safety and spoofing resistance: strip controls, bidi and zero-width formatting.
    value = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
    return " ".join(value.split())[:80].strip() or None


def branded_sender(validated_address: str) -> str:
    return str(Address(display_name="Revel Gifts", addr_spec=validated_address))


def render_invitation(*, sender_name: str | None, invite_url: str) -> InvitationEmail:
    parts = urlsplit(invite_url)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.path != "/app/invite"
        or parts.query
        or not parts.fragment.startswith("token=")
        or any(unicodedata.category(c).startswith("C") for c in invite_url)
    ):
        raise ValueError("Invalid invitation URL")
    name = safe_sender_name(sender_name)
    subject = f"{name} invited you to Revel Gifts" if name else "You're invited to Revel Gifts"
    introduction = (
        f"{name} invited you to Revel Gifts." if name else "You're invited to Revel Gifts."
    )
    byline = f"Sent by Revel Gifts on behalf of {name}." if name else "Sent by Revel Gifts."
    text = (
        "REVEL GIFTS\n\nGood gifts feel like you.\n\n" + introduction + "\n\n"
        "The assistants you already use can help build your gifting profile: interests, "
        "favorites and definite no-thank-yous, for the people who want to get your gifts right.\n\n"
        "Create Revel account:\n" + invite_url + "\n\n"
        "A few simple steps\n"
        "1. Join Revel using the email that received this invitation.\n"
        "2. Bring your assistant: connect one when you\u2019re ready. "
        "It can help build your profile, with your approval.\n"
        "3. Choose what to share and who can see it.\n\n"
        "Your profile, your call. Joining Revel won\u2019t share your profile. "
        "You decide what to share and who can see it.\n\n" + byline + "\n"
        "Secure sign-in verifies the email this invitation was sent to. "
        "If you weren't expecting this invitation, you can ignore it.\n"
    )
    intro, url = escape(introduction), escape(invite_url, quote=True)
    html = Template(
        Path(__file__).with_name("invitation_email.html").read_text(encoding="utf-8")
    ).substitute(subject=escape(subject), intro=intro, url=url, byline=escape(byline))
    return InvitationEmail(subject=subject, text=text, html=html)
