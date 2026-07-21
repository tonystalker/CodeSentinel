"""
tests/conftest.py
==================
Shared pytest configuration and fixtures.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Prevent accidental network calls in unit tests.
    Tests that genuinely need network should mark themselves with:
        @pytest.mark.network
    and call monkeypatch.undo() or use a different fixture scope.
    """
    # We don't block network in unit tests here — the embedder and LLM
    # tests use mocking. This fixture is a placeholder for future use.
    pass
