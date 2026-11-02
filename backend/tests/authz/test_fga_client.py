"""Unit tests for revel.auth.fga (no OpenFGA server needed) and bootstrap model comparison."""

import asyncio
import json
import threading
from typing import Any

import jwt
import pytest

from revel.auth import fga_bootstrap
from revel.auth.fga import (
    FgaConfigurationError,
    GoogleIdTokenProvider,
    OpenFgaServiceClient,
    RelationTuple,
    create_fga_client,
)
from revel.settings import Settings

pytestmark = pytest.mark.anyio


def _token(exp: float) -> str:
    return jwt.encode({"exp": int(exp), "aud": "x"}, "synthetic-test-key-32-bytes-long!", "HS256")


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def test_token_provider_caches_and_refreshes_before_expiry() -> None:
    clock = Clock(1_000.0)
    fetches: list[tuple[str, str]] = []
    loop_thread = threading.current_thread().name

    def fetch(audience: str) -> str:
        fetches.append((audience, threading.current_thread().name))
        return _token(clock.now + 3600)

    provider = GoogleIdTokenProvider(
        "https://openfga.example.run.app", fetch=fetch, clock=clock, refresh_margin=300
    )
    first = await provider.token()
    assert await provider.token() == first
    assert len(fetches) == 1
    assert fetches[0][0] == "https://openfga.example.run.app"
    # Fetched in a worker thread, not on the event loop.
    assert fetches[0][1] != loop_thread

    clock.now += 3600 - 299  # inside the refresh margin
    assert await provider.token() != first
    assert len(fetches) == 2
    assert first not in repr(provider)


async def test_token_provider_single_flight() -> None:
    calls = 0

    def fetch(audience: str) -> str:
        nonlocal calls
        calls += 1
        return _token(10_000)

    provider = GoogleIdTokenProvider("aud", fetch=fetch, clock=Clock(0.0))
    tokens = await asyncio.gather(*(provider.token() for _ in range(5)))
    assert len(set(tokens)) == 1
    assert calls == 1


async def test_auth_header_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    local = create_fga_client(Settings(_env_file=None))
    try:
        assert "headers" not in await local.request_options()
    finally:
        await local.close()

    monkeypatch.setattr("revel.auth.fga._fetch_google_id_token", lambda audience: _token(1e10))
    settings = Settings(
        _env_file=None, openfga_api_url="https://openfga.example.run.app/", openfga_google_auth=True
    )
    remote = create_fga_client(settings)
    try:
        options = await remote.request_options(consistency="HIGHER_CONSISTENCY")
        assert options["headers"]["Authorization"].startswith("Bearer ")
        assert options["consistency"] == "HIGHER_CONSISTENCY"
        assert remote._token_provider is not None
        assert remote._token_provider._audience == "https://openfga.example.run.app"
    finally:
        await remote.close()


class RecordingSdk:
    def __init__(self) -> None:
        self.writes: list[tuple[Any, dict[str, Any]]] = []

    async def write(self, body: Any, options: dict[str, Any]) -> None:
        self.writes.append((body, options))

    async def close(self) -> None:
        pass


async def test_writes_are_deduped_chunked_and_ignore_conflicts() -> None:
    sdk = RecordingSdk()
    client = OpenFgaServiceClient(sdk, store_id="s", authorization_model_id="m")  # type: ignore[arg-type]
    tuples = [RelationTuple(f"user:{i}", "owner", "profile:p") for i in range(150)]
    await client.write_tuples([*tuples, tuples[0]])
    assert [len(body.writes) for body, _ in sdk.writes] == [100, 50]
    conflict = sdk.writes[0][1]["conflict"]
    assert conflict.on_duplicate_writes.value == "ignore"
    assert conflict.on_missing_deletes.value == "ignore"

    await client.delete_tuples(tuples[:2])
    body, _ = sdk.writes[-1]
    assert body.writes is None
    assert len(body.deletes) == 2


async def test_unconfigured_client_fails_closed() -> None:
    client = OpenFgaServiceClient(RecordingSdk(), store_id=None, authorization_model_id=None)  # type: ignore[arg-type]
    with pytest.raises(FgaConfigurationError):
        await client.check(user="user:a", relation="contribute", object="profile:b")
    with pytest.raises(FgaConfigurationError):
        await client.write_tuples([RelationTuple("user:a", "owner", "profile:b")])


def test_model_body_ignores_server_defaults() -> None:
    model = fga_bootstrap.load_model(fga_bootstrap.MODEL_JSON_PATH)
    server_style = json.loads(json.dumps(model))
    server_style["id"] = "01ABC"
    server_style["conditions"] = {}
    for type_def in server_style["type_definitions"]:
        type_def.setdefault("relations", {})
        type_def.setdefault("metadata", {"module": "", "relations": {}})
    assert fga_bootstrap.model_body(server_style) == fga_bootstrap.model_body(model)

    changed = json.loads(json.dumps(model))
    changed["type_definitions"][1]["relations"]["acts_for"] = {"this": None}
    assert fga_bootstrap.model_body(changed) != fga_bootstrap.model_body(model)


def test_committed_model_json_matches_model_v1_relations() -> None:
    model = fga_bootstrap.load_model(fga_bootstrap.MODEL_JSON_PATH)
    relations = {t["type"]: sorted(t.get("relations", {})) for t in model["type_definitions"]}
    assert relations == {
        "user": ["delegate"],
        "agent": ["acts_for"],
        "profile": ["contribute", "owner", "owner_agent", "view_only_me"],
    }
