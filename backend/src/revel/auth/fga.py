"""OpenFGA client wrapper. Owned by F4 (authorization).

``create_fga_client(settings)`` builds the async client the app stores at ``app.state.fga``.
Callers use the small ``FgaClient`` protocol (idempotent tuple writes/deletes, checks, reads)
rather than the SDK directly, so tests can substitute an in-memory fake.

Service authentication: locally OpenFGA is unauthenticated. When ``OPENFGA_GOOGLE_AUTH`` is
true (Cloud Run to a private OpenFGA Cloud Run service), every request carries
``Authorization: Bearer <Google ID token>`` with audience ``OPENFGA_API_URL``. Tokens are
fetched off the event loop, cached until shortly before expiry and never logged.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import jwt
from openfga_sdk import ClientConfiguration, OpenFgaClient
from openfga_sdk.client.models import (
    ClientCheckRequest,
    ClientTuple,
    ClientWriteRequest,
    ClientWriteRequestOnDuplicateWrites,
    ClientWriteRequestOnMissingDeletes,
    ConflictOptions,
)
from openfga_sdk.models.consistency_preference import ConsistencyPreference
from openfga_sdk.models.read_request_tuple_key import ReadRequestTupleKey

from revel.settings import Settings

logger = logging.getLogger(__name__)

# OpenFGA's default per-request limit on tuple writes + deletes (OPENFGA_MAX_TUPLES_PER_WRITE).
MAX_TUPLES_PER_WRITE = 100
# Refresh Google ID tokens this many seconds before they expire.
TOKEN_REFRESH_MARGIN_SECONDS = 300.0
# Used only if a fetched token carries no readable ``exp`` claim.
TOKEN_FALLBACK_LIFETIME_SECONDS = 300.0


class FgaConfigurationError(RuntimeError):
    """OpenFGA store or model ID is not configured (run ``python -m revel.auth.fga_bootstrap``)."""


@dataclass(frozen=True, slots=True)
class RelationTuple:
    """One OpenFGA relationship tuple: ``user`` has ``relation`` on ``object``."""

    user: str
    relation: str
    object: str


@dataclass(frozen=True, slots=True)
class StoredTuple:
    """A tuple as read back from OpenFGA, with the time it was written."""

    tuple: RelationTuple
    written_at: datetime


class FgaClient(Protocol):
    """Operations the authorization service needs from OpenFGA.

    ``write_tuples`` ignores tuples that already exist and ``delete_tuples`` ignores tuples
    that do not, so hook retries are safe.
    """

    async def write_tuples(self, tuples: Iterable[RelationTuple]) -> None: ...

    async def delete_tuples(self, tuples: Iterable[RelationTuple]) -> None: ...

    async def check(self, *, user: str, relation: str, object: str) -> bool: ...

    async def read_tuples(
        self, *, relation: str | None = None, object: str | None = None, user: str | None = None
    ) -> list[RelationTuple]: ...

    async def read_stored_tuples(self) -> list["StoredTuple"]: ...

    @property
    def configured(self) -> bool:
        """Whether store and model IDs are set (checks and writes can run)."""
        ...

    async def close(self) -> None: ...


def _dedupe(tuples: Iterable[RelationTuple]) -> list[RelationTuple]:
    # OpenFGA rejects a request that names the same tuple twice.
    return list(dict.fromkeys(tuples))


def _chunks(items: Sequence[RelationTuple], size: int) -> Iterable[Sequence[RelationTuple]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _client_tuple(t: RelationTuple) -> ClientTuple:
    return ClientTuple(user=t.user, relation=t.relation, object=t.object)


def _token_expiry(token: str, now: float) -> float:
    """Read ``exp`` without verification; used only to schedule a refresh."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        return float(claims["exp"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return now + TOKEN_FALLBACK_LIFETIME_SECONDS


def _fetch_google_id_token(audience: str) -> str:
    """Blocking fetch via ADC: service-account file locally, metadata server on Cloud Run."""
    import google.auth.transport.urllib3
    import google.oauth2.id_token
    import urllib3

    request = google.auth.transport.urllib3.Request(urllib3.PoolManager())
    return google.oauth2.id_token.fetch_id_token(request, audience)


class GoogleIdTokenProvider:
    """Caches a Google ID token for one audience and refreshes it before expiry."""

    def __init__(
        self,
        audience: str,
        *,
        fetch: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.time,
        refresh_margin: float = TOKEN_REFRESH_MARGIN_SECONDS,
    ) -> None:
        self._audience = audience
        self._fetch = fetch
        self._clock = clock
        self._refresh_margin = refresh_margin
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"GoogleIdTokenProvider(audience={self._audience!r})"

    def _fresh(self) -> bool:
        return self._token is not None and self._clock() < self._expires_at - self._refresh_margin

    async def token(self) -> str:
        if self._fresh():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            if not self._fresh():
                # google-auth is synchronous; keep the event loop free while it fetches.
                fetch = self._fetch or _fetch_google_id_token
                token = await asyncio.to_thread(fetch, self._audience)
                self._token = token
                self._expires_at = _token_expiry(token, self._clock())
                logger.debug("Refreshed OpenFGA ID token")
            return self._token  # type: ignore[return-value]


class OpenFgaServiceClient:
    """``FgaClient`` backed by the async OpenFGA SDK."""

    def __init__(
        self,
        sdk: OpenFgaClient,
        *,
        store_id: str | None,
        authorization_model_id: str | None,
        token_provider: GoogleIdTokenProvider | None = None,
    ) -> None:
        self.sdk = sdk
        self.store_id = store_id
        self.authorization_model_id = authorization_model_id
        self._token_provider = token_provider

    async def request_options(self, **extra: Any) -> dict[str, Any]:
        """Fresh per-request SDK options (the SDK mutates them), with auth headers if enabled."""
        options: dict[str, Any] = dict(extra)
        if self._token_provider is not None:
            token = await self._token_provider.token()
            options["headers"] = {"Authorization": f"Bearer {token}"}
        return options

    @property
    def configured(self) -> bool:
        return bool(self.store_id and self.authorization_model_id)

    def _require_configured(self) -> None:
        if not self.configured:
            raise FgaConfigurationError(
                "OPENFGA_STORE_ID and OPENFGA_AUTHORIZATION_MODEL_ID must be set"
            )

    async def _write(self, *, writes: list[RelationTuple], deletes: list[RelationTuple]) -> None:
        self._require_configured()
        conflict = ConflictOptions(
            on_duplicate_writes=ClientWriteRequestOnDuplicateWrites.IGNORE,
            on_missing_deletes=ClientWriteRequestOnMissingDeletes.IGNORE,
        )
        body = ClientWriteRequest(
            writes=[_client_tuple(t) for t in writes] or None,
            deletes=[_client_tuple(t) for t in deletes] or None,
        )
        await self.sdk.write(body, await self.request_options(conflict=conflict))

    async def write_tuples(self, tuples: Iterable[RelationTuple]) -> None:
        for chunk in _chunks(_dedupe(tuples), MAX_TUPLES_PER_WRITE):
            await self._write(writes=list(chunk), deletes=[])

    async def delete_tuples(self, tuples: Iterable[RelationTuple]) -> None:
        for chunk in _chunks(_dedupe(tuples), MAX_TUPLES_PER_WRITE):
            await self._write(writes=[], deletes=list(chunk))

    async def check(self, *, user: str, relation: str, object: str) -> bool:
        self._require_configured()
        response = await self.sdk.check(
            ClientCheckRequest(user=user, relation=relation, object=object),
            # Bypass any check cache so revocation takes effect immediately.
            await self.request_options(consistency=ConsistencyPreference.HIGHER_CONSISTENCY),
        )
        return bool(response.allowed)

    async def read_tuples(
        self, *, relation: str | None = None, object: str | None = None, user: str | None = None
    ) -> list[RelationTuple]:
        """OpenFGA Read. ``object`` is ``type:id``, or ``type:`` (all of a type; needs ``user``)."""
        stored = await self._read(user=user, relation=relation, object=object)
        return [s.tuple for s in stored]

    async def read_stored_tuples(self) -> list[StoredTuple]:
        """Every tuple in the store with its write time (``fga_reconcile --prune``)."""
        return await self._read(user=None, relation=None, object=None)

    async def _read(
        self, *, user: str | None, relation: str | None, object: str | None
    ) -> list[StoredTuple]:
        self._require_configured()
        found: list[StoredTuple] = []
        continuation: str | None = None
        while True:
            extra: dict[str, Any] = {"consistency": ConsistencyPreference.HIGHER_CONSISTENCY}
            if continuation:
                extra["continuation_token"] = continuation
            response = await self.sdk.read(
                ReadRequestTupleKey(user=user, relation=relation, object=object),
                await self.request_options(**extra),
            )
            for item in response.tuples or []:
                key = item.key
                found.append(
                    StoredTuple(RelationTuple(key.user, key.relation, key.object), item.timestamp)
                )
            continuation = response.continuation_token or None
            if not continuation:
                return found

    async def close(self) -> None:
        await self.sdk.close()


def create_fga_client(settings: Settings) -> OpenFgaServiceClient:
    """Build the OpenFGA client from settings. Call inside the app lifespan (event loop)."""
    api_url = settings.openfga_api_url.rstrip("/")
    sdk = OpenFgaClient(
        ClientConfiguration(
            api_url=api_url,
            store_id=settings.openfga_store_id or None,
            authorization_model_id=settings.openfga_authorization_model_id or None,
        )
    )
    token_provider = GoogleIdTokenProvider(api_url) if settings.openfga_google_auth else None
    return OpenFgaServiceClient(
        sdk,
        store_id=settings.openfga_store_id or None,
        authorization_model_id=settings.openfga_authorization_model_id or None,
        token_provider=token_provider,
    )
