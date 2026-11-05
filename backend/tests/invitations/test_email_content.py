"""Canonical branded email content, hostile server names and capability preservation."""

from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest

from revel.services.invitation_email import render_invitation, safe_sender_name

EXPIRY = datetime(2026, 11, 12, 18, 30, tzinfo=UTC)
URL = 'https://revel.example/app/invite#token=a&b="quoted"'


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.tags, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.links.extend(value for key, value in attrs if key == "href")

    def handle_data(self, data):
        self.text.append(data)


def test_canonical_brand_and_semantic_plaintext_parity():
    result = render_invitation(sender_name="Leo Walker", expires_at=EXPIRY, invite_url=URL)
    assert result.subject == "Leo Walker invited you to Revel Gifts"
    parsed = Links()
    parsed.feed(result.html)
    assert parsed.links == [URL, URL]
    for phrase in [
        "Leo Walker invited you to Revel Gifts.",
        "View invitation",
        "A few simple steps",
        "with your approval.",
        "Accepting this invitation won\u2019t share your profile.",
        "You decide what to share and who can see it.",
        "November 12, 2026 at 18:30 UTC",
    ]:
        assert phrase in result.text
        assert phrase in "".join(parsed.text)
    for value in [
        "#f8f4ef",
        "#fffdfa",
        "#272120",
        "#61544e",
        "#e4d8d0",
        "#f2ac9e",
        "#813c31",
        "#f9e5df",
        "Georgia",
        "@media",
    ]:
        assert value in result.html
    assert 'bgcolor="#f2ac9e"' in result.html
    assert "font-weight:bold;color:#272120;text-decoration:none;" in result.html
    assert not ({"img", "script", "iframe", "form", "link"} & set(parsed.tags))
    assert "WorkOS" not in result.html
    assert "seven days" not in result.text
    assert URL in result.text


def test_hostile_name_is_header_safe_and_html_escaped():
    name = 'Leo\r\nBcc: other@example.test\u202e<script>"&</script>'
    result = render_invitation(sender_name=name, expires_at=EXPIRY, invite_url=URL)
    assert (
        "\r" not in result.subject and "\n" not in result.subject and "\u202e" not in result.subject
    )
    assert "<script>" not in result.html
    assert "&lt;script&gt;&quot;&amp;&lt;/script&gt;" in result.html
    assert '<script>"&</script>' in result.text
    assert len(safe_sender_name("x" * 500)) == 80


@pytest.mark.parametrize("name", [None, "", " \r\n\u202e\x00 "])
def test_missing_name_has_safe_generic_fallback(name):
    result = render_invitation(sender_name=name, expires_at=EXPIRY, invite_url=URL)
    assert result.subject == "You're invited to Revel Gifts"
    assert "None" not in result.text
    assert "wife" not in result.text.lower()


def test_actual_expiry_is_converted_to_utc_and_naive_dates_refused():
    result = render_invitation(
        sender_name="Leo",
        expires_at=EXPIRY.astimezone(timezone(timedelta(hours=-8))),
        invite_url=URL,
    )
    assert "November 12, 2026 at 18:30 UTC" in result.text
    with pytest.raises(ValueError, match="timezone"):
        render_invitation(sender_name="Leo", expires_at=EXPIRY.replace(tzinfo=None), invite_url=URL)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://evil@example.test/app/invite#token=x",
        "https://revel.example/app/invite?token=x",
        "https://revel.example/app/invite#token=x\r\n",
    ],
)
def test_invalid_url_never_reaches_html_or_headers(url):
    with pytest.raises(ValueError):
        render_invitation(sender_name="Leo", expires_at=EXPIRY, invite_url=url)
