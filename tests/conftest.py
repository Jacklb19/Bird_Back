"""Shared fixtures."""
import pytest

from birdnet_api.settings import get_settings


@pytest.fixture(autouse=True)
def fresh_settings():
    # Settings are cached per process; each test reads the environment it sets up, never a previous test's.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
