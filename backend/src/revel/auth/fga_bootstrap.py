"""OpenFGA store and model bootstrap. Owned by F4 (authorization).

Run explicitly, never at app startup::

    uv run python -m revel.auth.fga_bootstrap [--model PATH] [--store-name NAME]

Uses ``OPENFGA_API_URL`` (and ``OPENFGA_GOOGLE_AUTH``) from settings. The store is
``OPENFGA_STORE_ID`` when set, otherwise the single store named ``revel-gifts`` (created if
absent). The model is ``authorization/model.json``, the committed CLI transform of
``authorization/model.fga`` (``authorization/scripts/generate-model-json.sh``; checked by
``test-model.sh``), so this needs neither Docker nor a DSL parser. A new model version is
written only when it differs from the store's latest. Prints the resulting
``OPENFGA_STORE_ID`` and ``OPENFGA_AUTHORIZATION_MODEL_ID`` lines for configuration.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openfga_sdk.models.create_store_request import CreateStoreRequest

from revel.auth.fga import OpenFgaServiceClient, create_fga_client
from revel.settings import Settings, get_settings

STORE_NAME = "revel-gifts"
MODEL_JSON_PATH = Path(__file__).resolve().parents[4] / "authorization" / "model.json"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m revel.auth.fga_bootstrap")
    parser.add_argument("--model", type=Path, default=MODEL_JSON_PATH, help="model JSON path")
    parser.add_argument("--store-name", default=STORE_NAME, help="store name to find or create")
    args = parser.parse_args(argv)

    result = asyncio.run(
        bootstrap(get_settings(), model_path=args.model, store_name=args.store_name)
    )
    store_note = "created" if result.store_created else "existing"
    model_note = "written" if result.model_written else "unchanged"
    print(f"# store {store_note}, model {model_note}", file=sys.stderr)
    print(f"OPENFGA_STORE_ID={result.store_id}")
    print(f"OPENFGA_AUTHORIZATION_MODEL_ID={result.authorization_model_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
