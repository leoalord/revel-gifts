"""Fixtures for F4 authorization tests."""

import pytest
from authz_fakes import FakeFga


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "openfga: integration test against a real OpenFGA server (OPENFGA_TEST_API_URL)"
    )


@pytest.fixture
def fake_fga() -> FakeFga:
    return FakeFga()
