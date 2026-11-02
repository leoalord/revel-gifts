"""Process-wide authz configuration, module-level hooks, fail-closed rules and bootstrap CLI."""

import importlib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from authz_fakes import FakeFga, Principal, make_person
from fastapi.testclient import TestClient

from revel.app import create_app
from revel.auth import authz, fga_bootstrap
from revel.auth.authz import (
    AuthorizationDenied,
    ViewerTier,
    acts_for_tuple,
    can_contribute,
    configure_authz,
    delegate_tuple,
    delegation_tuples,
    owner_tuple,
    require_contribute,
    require_viewer,
    reset_authz,
    resolve_viewer_tier,
    subject_for,
)
from revel.auth.fga import FgaConfigurationError, OpenFgaServiceClient
from revel.settings import Settings

pytestmark = pytest.mark.anyio

HOOK_NAMES = ("on_user_created", "on_agent_delegated", "on_agent_disconnected")


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_authz()
    yield
    reset_authz()


def f2_hook(name: str) -> Any:
    """F2's lookup pattern."""
    return getattr(importlib.import_module("revel.auth.authz"), name)


# --- Module-level hooks -----------------------------------------------------------------------


def test_f2_lookup_finds_all_hooks() -> None:
    for name in HOOK_NAMES:
        hook = f2_hook(name)
        assert callable(hook)


async def test_module_hooks_delegate_to_configured_hooks(fake_fga: FakeFga) -> None:
    alex = make_person()
    fake_fga.tuples.add(owner_tuple(alex.user_id, alex.profile_id))
    configure_authz(fake_fga)
    agent_id = uuid4()
    await f2_hook("on_user_created")(user_id=alex.user_id)
    await f2_hook("on_agent_delegated")(
        user_id=alex.user_id, agent_id=agent_id, delegation_id=alex.delegation_id
    )
    assert fake_fga.tuples == {
        owner_tuple(alex.user_id, alex.profile_id),
        *delegation_tuples(alex.user_id, alex.delegation_id),
    }
    assert await can_contribute(fake_fga, alex.as_agent(agent_id), alex.profile_id)
    await f2_hook("on_agent_disconnected")(
        user_id=alex.user_id, delegation_id=alex.delegation_id, agent_id=agent_id
    )
    assert fake_fga.tuples == {owner_tuple(alex.user_id, alex.profile_id)}


@pytest.mark.parametrize("configured", [False, True], ids=["never-configured", "ids-unset"])
async def test_grant_hooks_warn_and_return_when_unconfigured(
    configured: bool, caplog: pytest.LogCaptureFixture
) -> None:
    unconfigured = FakeFga(configured=False)
    if configured:
        configure_authz(unconfigured)
    caplog.set_level(logging.WARNING, logger="revel.auth.authz")
    user_id = uuid4()
    assert await f2_hook("on_user_created")(user_id=user_id) is None
    assert (
        await f2_hook("on_agent_delegated")(
            user_id=user_id, agent_id=uuid4(), delegation_id=uuid4()
        )
        is None
    )
    assert unconfigured.calls == []
    warnings = [r for r in caplog.records if "not configured" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize("configured", [False, True], ids=["never-configured", "ids-unset"])
async def test_revoke_hook_raises_when_unconfigured(configured: bool) -> None:
    unconfigured = FakeFga(configured=False)
    if configured:
        configure_authz(unconfigured)
    with pytest.raises(FgaConfigurationError):
        await f2_hook("on_agent_disconnected")(user_id=uuid4(), delegation_id=uuid4())
    assert unconfigured.calls == []


def _hook_kwargs(name: str) -> dict[str, Any]:
    alex = make_person()
    kwargs: dict[str, Any] = {"user_id": alex.user_id}
    if name != "on_user_created":
        kwargs["delegation_id"] = alex.delegation_id
    if name == "on_agent_delegated":
        kwargs["agent_id"] = uuid4()
    return kwargs


@pytest.mark.parametrize("name", ["on_agent_delegated", "on_agent_disconnected"])
async def test_hooks_raise_when_configured_client_errors(name: str, fake_fga: FakeFga) -> None:
    configure_authz(fake_fga)
    fake_fga.error = RuntimeError("openfga unavailable")
    with pytest.raises(RuntimeError, match="openfga unavailable"):
        await f2_hook(name)(**_hook_kwargs(name))


async def test_user_created_makes_no_openfga_call(fake_fga: FakeFga) -> None:
    configure_authz(fake_fga)
    fake_fga.error = RuntimeError("openfga unavailable")
    await f2_hook("on_user_created")(**_hook_kwargs("on_user_created"))
    assert fake_fga.calls == []


class SlowFga(FakeFga):
    async def _slow(self) -> None:
        await anyio.sleep(30)

    async def write_tuples(self, tuples: Any) -> None:
        await self._slow()

    async def read_tuples(self, **kwargs: Any) -> list[Any]:
        await self._slow()
        return []


@pytest.mark.parametrize("name", ["on_agent_delegated", "on_agent_disconnected"])
async def test_hooks_time_out_promptly(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authz, "HOOK_TIMEOUT_SECONDS", 0.2)
    configure_authz(SlowFga())
    started = anyio.current_time()
    with pytest.raises(TimeoutError):
        await f2_hook(name)(**_hook_kwargs(name))
    assert anyio.current_time() - started < 2


def test_default_hook_timeout_is_three_seconds() -> None:
    assert authz.HOOK_TIMEOUT_SECONDS == 3.0


# --- Checks fail closed when unconfigured -----------------------------------------------------


@pytest.mark.parametrize("configured", [False, True], ids=["never-configured", "ids-unset"])
async def test_checks_raise_when_unconfigured(configured: bool) -> None:
    unconfigured = FakeFga(configured=False)
    # The owner tuple would allow, so a fake that skipped the guard would return True.
    alex = make_person()
    unconfigured.tuples.add(owner_tuple(alex.user_id, alex.profile_id))
    if configured:
        configure_authz(unconfigured)
    principal = alex.as_user()
    for client in (None, unconfigured):
        with pytest.raises(FgaConfigurationError):
            await can_contribute(client, principal, alex.profile_id)
        with pytest.raises(FgaConfigurationError):
            await require_contribute(client, principal, alex.profile_id)
        with pytest.raises(FgaConfigurationError):
            await resolve_viewer_tier(client, principal, alex.profile_id)
        with pytest.raises(FgaConfigurationError):
            await require_viewer(client, principal, alex.profile_id)
    assert unconfigured.calls == []


async def test_checks_use_configured_client_when_none_passed(fake_fga: FakeFga) -> None:
    alex = make_person()
    fake_fga.tuples.add(owner_tuple(alex.user_id, alex.profile_id))
    configure_authz(fake_fga)
    assert await can_contribute(None, alex.as_user(), alex.profile_id)
    assert await resolve_viewer_tier(None, alex.as_user(), alex.profile_id) is ViewerTier.only_me


async def test_unconfigured_real_client_check_raises() -> None:
    client = OpenFgaServiceClient(object(), store_id=None, authorization_model_id=None)  # type: ignore[arg-type]
    assert not client.configured
    with pytest.raises(FgaConfigurationError):
        await can_contribute(client, make_person().as_user(), uuid4())


# --- Principal invariants ---------------------------------------------------------------------


@pytest.mark.parametrize("via", ["mcp", "api"])
def test_agent_transport_without_delegation_is_denied(via: str) -> None:
    with pytest.raises(AuthorizationDenied):
        subject_for(Principal(user_id=uuid4(), via=via))  # type: ignore[arg-type]


def test_ui_with_delegation_is_denied() -> None:
    principal = Principal(user_id=uuid4(), agent_id=uuid4(), delegation_id=uuid4(), via="ui")
    with pytest.raises(AuthorizationDenied):
        subject_for(principal)


# --- Disconnect ordering ----------------------------------------------------------------------


async def test_disconnect_deletes_all_acts_for_and_delegate_of_the_delegation(
    fake_fga: FakeFga,
) -> None:
    alex, other = make_person(), make_person()
    # Stray tuples on alex's delegation object naming another user must be revoked too.
    fake_fga.tuples |= {
        acts_for_tuple(alex.user_id, alex.delegation_id),
        acts_for_tuple(other.user_id, alex.delegation_id),
        delegate_tuple(alex.user_id, alex.delegation_id),
        delegate_tuple(other.user_id, alex.delegation_id),
        *delegation_tuples(other.user_id, other.delegation_id),
    }
    configure_authz(fake_fga)
    await authz.on_agent_disconnected(user_id=alex.user_id, delegation_id=alex.delegation_id)
    assert fake_fga.tuples == {
        *delegation_tuples(other.user_id, other.delegation_id),
    }


# --- Consistency ------------------------------------------------------------------------------


class ConsistencySdk:
    def __init__(self) -> None:
        self.options: list[tuple[str, dict[str, Any]]] = []

    async def check(self, body: Any, options: dict[str, Any]) -> Any:
        self.options.append(("check", options))
        return type("R", (), {"allowed": True})()

    async def read(self, body: Any, options: dict[str, Any]) -> Any:
        self.options.append(("read", options))
        return type("R", (), {"tuples": [], "continuation_token": ""})()


async def test_check_and_read_use_higher_consistency() -> None:
    sdk = ConsistencySdk()
    client = OpenFgaServiceClient(sdk, store_id="s", authorization_model_id="m")  # type: ignore[arg-type]
    await client.check(user="user:a", relation="contribute", object="profile:b")
    await client.read_tuples(relation="acts_for", object="agent:x")
    assert [(name, opts.get("consistency")) for name, opts in sdk.options] == [
        ("check", "HIGHER_CONSISTENCY"),
        ("read", "HIGHER_CONSISTENCY"),
    ]


# --- App lifespan -----------------------------------------------------------------------------


def test_sequential_apps_do_not_share_closed_client(settings: Settings) -> None:
    seen: list[Any] = []
    for _ in range(2):
        app = create_app(settings)
        with TestClient(app, base_url="http://localhost:8000") as client:
            assert client.get("/health").status_code == 200
            assert authz._state is not None
            assert authz._state.fga is app.state.fga
            seen.append(app.state.fga)
        # Shutdown resets the module-level state.
        assert authz._state is None
    assert seen[0] is not seen[1]


# --- Bootstrap CLI ----------------------------------------------------------------------------


def _model(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    return path


def test_model_path_precedence(tmp_path: Path) -> None:
    cli = _model(tmp_path, "cli.json")
    env_model = _model(tmp_path, "env.json")
    cwd = tmp_path / "app" / "backend"
    cwd.mkdir(parents=True)
    image_model = _model(tmp_path, "app/authorization/model.json")
    env = {fga_bootstrap.MODEL_PATH_ENV: str(env_model)}

    assert fga_bootstrap.resolve_model_path(cli, env=env, cwd=cwd) == cli
    assert fga_bootstrap.resolve_model_path(None, env=env, cwd=cwd) == env_model
    assert fga_bootstrap.resolve_model_path(None, env={}, cwd=cwd) == image_model.resolve()
    # No image layout: fall back to the repository path next to the package.
    elsewhere = tmp_path / "x" / "y"
    elsewhere.mkdir(parents=True)
    assert (
        fga_bootstrap.resolve_model_path(None, env={}, cwd=elsewhere)
        == fga_bootstrap.MODEL_JSON_PATH.resolve()
    )


def test_model_path_explicit_missing_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(fga_bootstrap.BootstrapError):
        fga_bootstrap.resolve_model_path(tmp_path / "missing.json", env={}, cwd=tmp_path)
    env = {fga_bootstrap.MODEL_PATH_ENV: str(tmp_path / "missing.json")}
    with pytest.raises(fga_bootstrap.BootstrapError):
        fga_bootstrap.resolve_model_path(None, env=env, cwd=tmp_path)


def test_model_path_none_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fga_bootstrap, "MODEL_JSON_PATH", tmp_path / "nope" / "model.json")
    with pytest.raises(fga_bootstrap.BootstrapError, match="not found"):
        fga_bootstrap.resolve_model_path(None, env={}, cwd=tmp_path)


def test_main_exits_nonzero_without_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fga_bootstrap, "get_settings", lambda: Settings(_env_file=None))
    assert fga_bootstrap.main(["--model", str(tmp_path / "missing.json")]) != 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "not found" in err


def test_main_stdout_is_only_the_two_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_bootstrap(settings: Settings, **_: Any) -> fga_bootstrap.BootstrapResult:
        return fga_bootstrap.BootstrapResult("STORE", "MODEL", True, True)

    monkeypatch.setattr(fga_bootstrap, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(fga_bootstrap, "bootstrap", fake_bootstrap)
    assert fga_bootstrap.main([]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "OPENFGA_STORE_ID=STORE",
        "OPENFGA_AUTHORIZATION_MODEL_ID=MODEL",
    ]
    assert "store created" in err


def test_check_unconfigured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fga_bootstrap, "get_settings", lambda: Settings(_env_file=None))
    assert fga_bootstrap.main(["--check"]) == 0
    assert capsys.readouterr().out.strip() == "unconfigured"


def test_check_unreachable_is_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        _env_file=None,
        openfga_api_url="http://127.0.0.1:9",  # discard port: connection refused
        openfga_store_id="01M6DFD52PSCT9XZ22XF3VY9K4",
        openfga_authorization_model_id="01M6DFE8C5TDTSBCGBA2HK4M0Q",
    )
    monkeypatch.setattr(fga_bootstrap, "get_settings", lambda: settings)
    assert fga_bootstrap.main(["--check"]) != 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("unhealthy:")


def test_check_uses_app_client_path(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Settings] = []
    real = fga_bootstrap.create_fga_client

    def spy(settings: Settings) -> OpenFgaServiceClient:
        created.append(settings)
        return real(settings)

    monkeypatch.setattr(fga_bootstrap, "create_fga_client", spy)
    monkeypatch.setattr(fga_bootstrap, "get_settings", lambda: Settings(_env_file=None))
    assert fga_bootstrap.main(["--check"]) == 0
    assert len(created) == 1
