"""Fixtures for OEC Tariff tests."""

import asyncio

import aiohttp
import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integration loading for every test in this suite."""
    yield


@pytest.fixture(scope="session", autouse=True)
def _warm_up_aiohttp_before_any_test():
    """Absorb a one-time lazy-init cost (a background thread the first real
    aiohttp.ClientSession() in the process spins up) before any individual
    test's before/after thread-leak snapshot runs.

    Without this, pytest-homeassistant-custom-component's teardown check
    (see test_config_flow.py's own module docstring) flags that thread as
    "new" for whichever test happens to be first to create a real session --
    deterministic, but only an artifact of test ordering, not a real leak
    tied to any test's own code."""

    async def _warm():
        async with aiohttp.ClientSession():
            pass

    asyncio.run(_warm())
