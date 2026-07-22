"""Fixtures for OEC Tariff tests."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integration loading for every test in this suite."""
    yield
