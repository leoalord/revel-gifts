"""OpenAPI documents the bearer scheme on CurrentPrincipal routes only; 401s are unchanged."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from revel.api import routers
from revel.auth.deps import current_principal
from revel.auth.ui import current_ui_principal

from .conftest import RESOURCE_METADATA_URL

BEARER = [{"bearerAuth": []}]
METHODS = {"get", "put", "post", "patch", "delete"}


def _calls(dependant: Dependant) -> set[Any]:
    found = {dependant.call}
    for sub in dependant.dependencies:
        found |= _calls(sub)
    return found


def _api_routes(routes: list[Any], prefix: str = "") -> list[tuple[str, APIRoute]]:
    """Flatten routes, following nested ``include_router`` (FastAPI keeps them as branches)."""
    found: list[tuple[str, APIRoute]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append((prefix + route.path, route))
        elif (included := getattr(route, "original_router", None)) is not None:
            found += _api_routes(included.routes, prefix + route.include_context.prefix)
    return found


def _routes_using(dependency: Any) -> set[tuple[str, str]]:
    """(method, path) of every schema route in ``revel.api.routers()`` depending on it."""
    ops: set[tuple[str, str]] = set()
    for router in routers():
        for path, route in _api_routes(router.routes):
            if route.include_in_schema and dependency in _calls(route.dependant):
                ops |= {(m.lower(), path) for m in route.methods}
    return ops


def _operations(schema: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (method, path): op
        for path, item in schema["paths"].items()
        for method, op in item.items()
        if method in METHODS
    }


def test_openapi_declares_bearer_scheme(auth_client: TestClient) -> None:
    scheme = auth_client.get("/openapi.json").json()["components"]["securitySchemes"]["bearerAuth"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "JWT"
    assert "https://api.revel.gifts/mcp" in scheme["description"]


def test_only_current_principal_operations_require_bearer(auth_client: TestClient) -> None:
    ops = _operations(auth_client.get("/openapi.json").json())
    bearer_ops = _routes_using(current_principal)
    cookie_ops = _routes_using(current_ui_principal)

    assert ("get", "/me") in bearer_ops
    assert any(path.startswith("/api/v1/profile") for _, path in bearer_ops)
    assert any("question" in path for _, path in bearer_ops)
    assert cookie_ops and not bearer_ops & cookie_ops

    for key, op in ops.items():
        if key in bearer_ops:
            assert op.get("security") == BEARER, key
        else:
            assert "security" not in op, key
    assert bearer_ops <= ops.keys()
    assert "/health" not in {path for _, path in ops}
    assert not any(path.startswith(("/api/agents", "/auth/")) for _, path in bearer_ops)


def test_missing_or_non_bearer_header_401_unchanged(auth_client: TestClient) -> None:
    expected = f'Bearer resource_metadata="{RESOURCE_METADATA_URL}"'
    for headers in ({}, {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "Token x"}):
        response = auth_client.get("/me", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "Authentication required"}
        assert response.headers["www-authenticate"] == expected


@pytest.mark.parametrize("header", ["Bearer", "Bearer not-a-jwt", "bearer not-a-jwt"])
def test_malformed_bearer_401_unchanged(auth_client: TestClient, header: str) -> None:
    response = auth_client.get("/me", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired access token"}
    assert response.headers["www-authenticate"] == (
        'Bearer error="invalid_token", error_description="Invalid or expired token", '
        f'resource_metadata="{RESOURCE_METADATA_URL}"'
    )
