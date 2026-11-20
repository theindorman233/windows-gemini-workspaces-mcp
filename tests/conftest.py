import pytest


@pytest.fixture(autouse=True)
def _default_test_oauth_env(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_BASE", "https://fixture.example.invalid")
    monkeypatch.setenv("MCP_GITHUB_CLIENT_ID", "fixture-client-id")
    monkeypatch.setenv("MCP_GITHUB_OWNER_ID", "fixture-owner-id")