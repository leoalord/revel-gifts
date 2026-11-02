"""OpenFGA store and model bootstrap. Owned by F4 (authorization).

Run explicitly, never at app startup::

    uv run python -m revel.auth.fga_bootstrap [--model PATH] [--store-name NAME]
    uv run python -m revel.auth.fga_bootstrap --check

Uses ``OPENFGA_API_URL`` (and ``OPENFGA_GOOGLE_AUTH``) from settings, through the same client
and ID-token path as the app. The store is ``OPENFGA_STORE_ID`` when set, otherwise the single
store named ``revel-gifts`` (created if absent). The model is ``model.json``, the committed CLI
transform of ``authorization/model.fga`` (``authorization/scripts/generate-model-json.sh``;
checked by ``test-model.sh``), so this needs neither Docker nor a DSL parser. A new model
version is written only when it differs from the store's latest.

Model path precedence: ``--model``, then ``OPENFGA_MODEL_PATH``, then
``../authorization/model.json`` relative to the working directory (image layout: WORKDIR
``/app/backend``, model at ``/app/authorization``), then the repository path next to this
package. An explicit ``--model`` or ``OPENFGA_MODEL_PATH`` that does not exist is an error
rather than a fallback, and so is finding no model at all.

stdout carries only ``OPENFGA_STORE_ID=...`` and ``OPENFGA_AUTHORIZATION_MODEL_ID=...``;
progress goes to stderr.

``--check`` is a read-only probe: prints ``unconfigured`` (exit 0) when the store or model ID
is unset, ``healthy`` (exit 0) when both exist on a reachable server, and exits non-zero
otherwise.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openfga_sdk.models.create_store_request import CreateStoreRequest

from revel.auth.fga import OpenFgaServiceClient, create_fga_client
from revel.settings import Settings, get_settings

STORE_NAME = "revel-gifts"
MODEL_JSON_PATH = Path(__file__).resolve().parents[4] / "authorization" / "model.json"
MODEL_PATH_ENV = "OPENFGA_MODEL_PATH"


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    store_id: str
    authorization_model_id: str
    store_created: bool
    model_written: bool


_EMPTY: tuple[Any, ...] = (None, "", [], {})


def _normalize(value: Any) -> Any:
    """Drop unset fields so server output and CLI JSON compare equal regardless of defaults.

    The server fills defaults (``""`` module/condition, ``{}`` relations); ``this: {}`` is the
    one meaningful empty value and is kept.
    """
    if isinstance(value, dict):
        items = {k: _normalize(v) for k, v in value.items()}
        return {
            k: v for k, v in items.items() if v is not None and (k == "this" or v not in _EMPTY)
        }
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def model_body(model: dict[str, Any]) -> dict[str, Any]:
    """The comparable part of an authorization model."""
    return _normalize(
        {
            "schema_version": model.get("schema_version"),
            "type_definitions": model.get("type_definitions"),
            "conditions": model.get("conditions") or None,
        }
    )


def resolve_model_path(
    cli_path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Pick the model JSON by precedence (see module docstring); raise if none exists."""
    env = os.environ if env is None else env
    explicit = cli_path or (Path(env[MODEL_PATH_ENV]) if env.get(MODEL_PATH_ENV) else None)
    if explicit is not None:
        if not explicit.is_file():
            raise BootstrapError(f"Model file not found: {explicit}")
        return explicit
    candidates = [
        (cwd or Path.cwd()) / ".." / "authorization" / "model.json",
        MODEL_JSON_PATH,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise BootstrapError(
        "authorization/model.json not found; pass --model or set " + MODEL_PATH_ENV
    )


def load_model(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


async def _find_or_create_store(client: OpenFgaServiceClient, store_name: str) -> tuple[str, bool]:
    sdk = client.sdk
    if client.store_id:
        sdk.set_store_id(client.store_id)
        await sdk.get_store(await client.request_options())
        return client.store_id, False

    matches: list[str] = []
    continuation: str | None = None
    while True:
        extra: dict[str, Any] = {"name": store_name, "page_size": 100}
        if continuation:
            extra["continuation_token"] = continuation
        response = await sdk.list_stores(await client.request_options(**extra))
        matches += [store.id for store in response.stores or [] if store.name == store_name]
        continuation = response.continuation_token or None
        if not continuation:
            break
    if len(matches) > 1:
        raise BootstrapError(f"{len(matches)} stores named {store_name!r}; set OPENFGA_STORE_ID")
    if matches:
        store_id = matches[0]
        created = False
    else:
        response = await sdk.create_store(
            CreateStoreRequest(name=store_name), await client.request_options()
        )
        store_id = response.id
        created = True
    sdk.set_store_id(store_id)
    return store_id, created


async def bootstrap(
    settings: Settings,
    *,
    model_path: Path = MODEL_JSON_PATH,
    store_name: str = STORE_NAME,
) -> BootstrapResult:
    model = load_model(model_path)
    client = create_fga_client(settings)
    try:
        store_id, store_created = await _find_or_create_store(client, store_name)
        sdk = client.sdk
        latest = await sdk.read_authorization_models(await client.request_options(page_size=1))
        current = (latest.authorization_models or [None])[0]
        if current is not None and model_body(current.to_dict(serialize=True)) == model_body(model):
            return BootstrapResult(store_id, current.id, store_created, model_written=False)
        # The SDK serializes a plain dict body (the committed JSON) as-is.
        written = await sdk.write_authorization_model(
            model,  # type: ignore[arg-type]
            await client.request_options(),
        )
        return BootstrapResult(
            store_id, written.authorization_model_id, store_created, model_written=True
        )
    finally:
        await client.close()


async def check(settings: Settings) -> str:
    """Read-only probe: ``unconfigured`` or ``healthy``; raises if the store or model is missing."""
    client = create_fga_client(settings)
    try:
        if not client.configured:
            return "unconfigured"
        await client.sdk.get_store(await client.request_options())
        await client.sdk.read_authorization_model(
            await client.request_options(authorization_model_id=client.authorization_model_id)
        )
        return "healthy"
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m revel.auth.fga_bootstrap")
    parser.add_argument("--model", type=Path, default=None, help="model JSON path")
    parser.add_argument("--store-name", default=STORE_NAME, help="store name to find or create")
    parser.add_argument("--check", action="store_true", help="read-only health probe")
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.check:
        try:
            print(asyncio.run(check(settings)))
        except Exception as exc:
            # Exception text may carry IDs; the class name is enough for a probe.
            print(f"unhealthy: {type(exc).__name__}", file=sys.stderr)
            return 1
        return 0

    try:
        model_path = resolve_model_path(args.model)
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"# model {model_path}", file=sys.stderr)
    result = asyncio.run(bootstrap(settings, model_path=model_path, store_name=args.store_name))
    store_note = "created" if result.store_created else "existing"
    model_note = "written" if result.model_written else "unchanged"
    print(f"# store {store_note}, model {model_note}", file=sys.stderr)
    print(f"OPENFGA_STORE_ID={result.store_id}")
    print(f"OPENFGA_AUTHORIZATION_MODEL_ID={result.authorization_model_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
