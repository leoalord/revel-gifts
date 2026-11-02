"""Muse Spark client: Revel's internal model for question routing. Owned by F7.

Calls the Fireworks OpenAI-compatible chat completions endpoint with async httpx. Muse Spark is
a reasoning model: the answer arrives in ``message.content`` (reasoning separately in
``reasoning_content``) and reasoning consumes tokens, so callers pass ``max_tokens`` of at least
``MIN_MAX_TOKENS`` and ``reasoning_effort`` is ``low`` (default effort can exhaust the budget
before answering). Output is requested as JSON only; code fences are tolerated.

The client is "unavailable" when ``muse_spark_provider`` is ``meta`` (no Meta API client yet) or
the Fireworks key is missing; routing then uses its deterministic fallback.

Logging: only status, latency and error class. Never the key, prompts (they contain the private
purpose) or raw model output. Every call is hard-bounded by ``muse_spark_timeout_seconds``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import anyio
import httpx
from pydantic import SecretStr

logger = logging.getLogger(__name__)

FIREWORKS_CHAT_COMPLETIONS_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
DEFAULT_FIREWORKS_MODEL = "accounts/fireworks/models/muse-spark-open"
MIN_MAX_TOKENS = 800
# Measured 2 Nov 2026 on the routing prompt: default effort spent all 1000 tokens reasoning
# (~11.6s, no answer); "low" answered valid JSON in ~3s with ~250 tokens.
REASONING_EFFORT = "low"
DEFAULT_TIMEOUT_SECONDS = 8.0

_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


class MuseSparkError(Exception):
    """A model call failed; ``reason`` is a short code safe to log and store."""

    reason = "error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.reason)


class MuseSparkUnavailable(MuseSparkError):
    reason = "unavailable"


class MuseSparkTimeout(MuseSparkError):
    reason = "timeout"


class MuseSparkHTTPError(MuseSparkError):
    reason = "http_error"


class MuseSparkMalformed(MuseSparkError):
    reason = "malformed"


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse a JSON object from model content, tolerating code fences and surrounding prose."""
    text = _FENCE.sub("", content.strip()).strip()
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise MuseSparkMalformed() from None
        try:
            value = json.loads(text[start : end + 1])
        except ValueError:
            raise MuseSparkMalformed() from None
    if not isinstance(value, dict):
        raise MuseSparkMalformed()
    return value


class MuseSparkClient:
    """Async Muse Spark chat client. ``transport`` lets tests inject an httpx mock transport."""

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        model: str | None = None,
        provider: str = "fireworks",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        url: str = FIREWORKS_CHAT_COMPLETIONS_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model or DEFAULT_FIREWORKS_MODEL
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self._url = url
        self._transport = transport

    @classmethod
    def from_settings(
        cls, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> MuseSparkClient:
        return cls(
            api_key=getattr(settings, "fireworks_api_key", None),
            model=getattr(settings, "fireworks_model", None),
            provider=getattr(settings, "muse_spark_provider", "fireworks"),
            timeout_seconds=getattr(settings, "muse_spark_timeout_seconds", None)
            or DEFAULT_TIMEOUT_SECONDS,
            transport=transport,
        )

    @property
    def available(self) -> bool:
        return (
            self.provider == "fireworks"
            and self._api_key is not None
            and bool(self._api_key.get_secret_value())
        )

    def __repr__(self) -> str:  # never include the key
        return f"MuseSparkClient(provider={self.provider!r}, model={self.model!r})"

    async def complete_json(
        self, *, system: str, user: str, max_tokens: int = MIN_MAX_TOKENS
    ) -> dict[str, Any]:
        """One chat completion parsed as a JSON object. Raises ``MuseSparkError`` subclasses."""
        if not self.available or self._api_key is None:
            raise MuseSparkUnavailable()
        payload = {
            "model": self.model,
            "max_tokens": max(max_tokens, MIN_MAX_TOKENS),
            "temperature": 0,
            "reasoning_effort": REASONING_EFFORT,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Accept": "application/json",
        }
        started = time.monotonic()
        status: int | str = "-"
        try:
            with anyio.fail_after(self.timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=self.timeout_seconds
                ) as http:
                    response = await http.post(self._url, json=payload, headers=headers)
            status = response.status_code
            if response.status_code != 200:
                raise MuseSparkHTTPError()
            try:
                body = response.json()
                content = body["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise MuseSparkMalformed() from None
            if not isinstance(content, str) or not content.strip():
                raise MuseSparkMalformed()
            result = parse_json_object(content)
        except TimeoutError:
            status = "timeout"
            raise MuseSparkTimeout() from None
        except httpx.TimeoutException:
            status = "timeout"
            raise MuseSparkTimeout() from None
        except httpx.HTTPError:
            status = "transport_error"
            raise MuseSparkHTTPError() from None
        finally:
            logger.info(
                "muse_spark call status=%s latency_ms=%d",
                status,
                int((time.monotonic() - started) * 1000),
            )
        return result
