"""Shared test setup."""

import pytest

from jarvis.integrations import credentials


@pytest.fixture(autouse=True)
def isolated_secret_store() -> credentials.MemoryStore:
    """Tests never read or write the real Windows Credential Manager."""
    store = credentials.MemoryStore()
    credentials.set_default_store(store)
    yield store
    credentials.set_default_store(None)
