import asyncio

from types import SimpleNamespace

import pytest
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from starlette.testclient import TestClient

from oauth_probe import create_probe, resolve_probe_path

TEST_PUBLIC_BASE = "https://fixture.example.invalid"
TEST_PUBLIC_HOST = "fixture.example.invalid"


@pytest.mark.parametrize("subject,accepted", [("fixture-owner-id", True), ("999999999", False), (None, False)])
def test_provider_requires_owner(monkeypatch, subject, accepted):
    async def verified_token(provider, token):
        return AccessToken(token=token, client_id="test", scopes=[], subject=subject)

    monkeypatch.setattr(GitHubProvider, "load_access_token", verified_token)
    provider = create_probe("fixture-only").auth
    assert (asyncio.run(provider.load_access_token("fixture")) is not None) is accepted


def test_probe_only_and_public_oauth_discovery():
    server = create_probe("fixture-only")
    assert [tool.name for tool in asyncio.run(server.list_tools())] == ["probe"]
    with TestClient(server.http_app()) as client:
        denied = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert denied.status_code == 401
        assert f'{TEST_PUBLIC_BASE}/.well-known/oauth-protected-resource/mcp' in denied.headers["www-authenticate"]
        resource = client.get("/.well-known/oauth-protected-resource/mcp")
        assert resource.status_code == 200
        assert resource.json()["resource"] == f"{TEST_PUBLIC_BASE}/mcp"
        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert metadata.json()["registration_endpoint"] == f"{TEST_PUBLIC_BASE}/register"
        assert metadata.json()["code_challenge_methods_supported"] == ["S256"]


def test_funnel_host_and_origin_are_allowed_without_disabling_guard():
    app = create_probe("fixture-only").http_app(
        host_origin_protection=True,
        allowed_hosts=[TEST_PUBLIC_HOST], allowed_origins=[TEST_PUBLIC_BASE],
    )
    path = "/mcp"
    with TestClient(app) as client:
        assert client.post(path, headers={"Host": TEST_PUBLIC_HOST, "Origin": TEST_PUBLIC_BASE}).status_code == 401
        assert client.post(path, headers={"Host": "unrelated.invalid"}).status_code == 421
        assert client.post(path, headers={"Host": TEST_PUBLIC_HOST, "Origin": "https://unrelated.invalid"}).status_code == 403


@pytest.mark.parametrize("environment", ["", "-sandbox", "-test"])
@pytest.mark.parametrize("route", ["a", "r"])
def test_google_redirect_registration(environment, route):
    redirect = f"https://oauth-redirect{environment}.googleusercontent.com/{route}/fixture"
    with TestClient(create_probe("fixture-only").http_app()) as client:
        response = client.post("/register", json={
            "redirect_uris": [redirect], "application_type": "web",
            "token_endpoint_auth_method": "none",
        })
    assert response.status_code == 201
    assert response.json()["redirect_uris"] == [redirect]


def test_registration_rejection_logs_host_only(caplog):
    with TestClient(create_probe("fixture-only").http_app()) as client:
        response = client.post("/register", json={
            "redirect_uris": ["https://unrelated.invalid/callback?state=fixture-private"],
            "application_type": "web", "token_endpoint_auth_method": "none",
        })
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"
    assert "unrelated.invalid" in caplog.text
    assert "fixture-private" not in caplog.text


def test_probe_path_override_and_protected_resource_route(tmp_path, monkeypatch):
    def make_options(**overrides):
        base = {"live_workspaces_store": None, "workspace_store": None, "edit_workspace": None,
                "synthetic_read": False}
        base.update(overrides)
        return SimpleNamespace(**base)

    monkeypatch.delenv("MCP_PROBE_PATH", raising=False)
    assert resolve_probe_path(make_options()) == "/mcp"
    assert resolve_probe_path(make_options(synthetic_read=True)) == "/mcp-fixture"
    assert resolve_probe_path(make_options(edit_workspace=tmp_path)) == "/mcp-edit-fixture"
    assert resolve_probe_path(make_options(workspace_store=tmp_path)) == "/mcp-workspaces-v2"
    live_options = make_options(live_workspaces_store=tmp_path)
    assert resolve_probe_path(live_options) == "/mcp-workspaces-live"
    for blank in ("", "   "):
        monkeypatch.setenv("MCP_PROBE_PATH", blank)
        assert resolve_probe_path(live_options) == "/mcp-workspaces-live"
    for invalid in ("no-leading-slash", "//mcp-live-2", "/mcp-live-2?x=1", "/mcp live"):
        monkeypatch.setenv("MCP_PROBE_PATH", invalid)
        with pytest.raises(ValueError, match="single path"):
            resolve_probe_path(live_options)
    monkeypatch.setenv("MCP_PROBE_PATH", "/mcp-live-2")
    resolved = resolve_probe_path(live_options)
    assert resolved == "/mcp-live-2"
    app = create_probe("fixture-only", workspace_store=tmp_path, live=True).http_app(path=resolved)
    with TestClient(app) as client:
        denied = client.post(resolved, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert denied.status_code == 401
        assert f"{TEST_PUBLIC_BASE}/.well-known/oauth-protected-resource{resolved}" in denied.headers["www-authenticate"]
        resource = client.get(f"/.well-known/oauth-protected-resource{resolved}")
        assert resource.status_code == 200
        assert resource.json()["resource"] == f"{TEST_PUBLIC_BASE}{resolved}"


def test_live_probe_persists_oauth_clients_and_uses_long_ttls(tmp_path):
    first = create_probe("fixture-only", workspace_store=tmp_path, live=True)
    assert first.auth._fastmcp_access_token_expiry_seconds == 30 * 86400
    assert first.auth._fallback_refresh_token_expiry_seconds == 180 * 86400
    assert first.auth._token_expiry_threshold_seconds == 300
    redirect = "https://oauth-redirect.googleusercontent.com/r/persist-test"
    with TestClient(first.http_app(path="/mcp")) as client:
        registered = client.post("/register", json={
            "redirect_uris": [redirect], "application_type": "web",
            "token_endpoint_auth_method": "none",
        })
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]

    restarted = create_probe("fixture-only", workspace_store=tmp_path, live=True)
    assert restarted.auth._jwt_signing_key == first.auth._jwt_signing_key
    loaded = asyncio.run(restarted.auth.get_client(client_id))
    assert loaded is not None
    assert [str(uri) for uri in loaded.redirect_uris] == [redirect]


@pytest.mark.parametrize("flag", ["0", "false", "FALSE"])
def test_dcr_disabled_blocks_new_registration(monkeypatch, caplog, flag):
    monkeypatch.setenv("MCP_ALLOW_DCR", flag)
    redirect = "https://oauth-redirect.googleusercontent.com/r/dcr-disabled"
    with caplog.at_level("WARNING"), TestClient(create_probe("fixture-only").http_app()) as client:
        response = client.post("/register", json={
            "redirect_uris": [redirect],
            "application_type": "web",
            "token_endpoint_auth_method": "none",
        })
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"
    assert "dynamic client registration is disabled" in caplog.text


@pytest.mark.parametrize("live", [False, True])
def test_dcr_client_limit_rejects_excess_registrations(monkeypatch, caplog, tmp_path, live):
    monkeypatch.setenv("MCP_ALLOW_DCR", "1")
    monkeypatch.setenv("MCP_MAX_REGISTERED_CLIENTS", "2")
    server = create_probe("fixture-only", workspace_store=tmp_path if live else None, live=live)
    app = server.http_app(path="/mcp") if live else server.http_app()
    with caplog.at_level("WARNING"), TestClient(app) as client:
        for idx in range(2):
            resp = client.post("/register", json={
                "redirect_uris": [f"https://oauth-redirect.googleusercontent.com/r/limit-{idx}"],
                "application_type": "web",
                "token_endpoint_auth_method": "none",
            })
            assert resp.status_code == 201
        rejected = client.post("/register", json={
            "redirect_uris": ["https://oauth-redirect.googleusercontent.com/r/limit-overflow"],
            "application_type": "web",
            "token_endpoint_auth_method": "none",
        })
    assert rejected.status_code == 400
    assert rejected.json()["error"] == "invalid_client_metadata"
    assert "registered client limit reached (2/2)" in caplog.text


def test_live_mode_registers_list_workspace_dir_tool(tmp_path):
    server = create_probe("fixture-only", workspace_store=tmp_path, live=True)
    tool_names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "list_workspace_dir" in tool_names
    assert len(tool_names) == 13


def test_fail_closed_rejects_missing_or_placeholder_config(monkeypatch):
    monkeypatch.delenv("MCP_GITHUB_OWNER_ID", raising=False)
    with pytest.raises(ValueError, match="MCP_GITHUB_OWNER_ID"):
        create_probe("fixture-only")

    monkeypatch.setenv("MCP_GITHUB_OWNER_ID", "123456789")
    with pytest.raises(ValueError, match="MCP_GITHUB_OWNER_ID"):
        create_probe("fixture-only")

    monkeypatch.setenv("MCP_GITHUB_OWNER_ID", "fixture-owner-id")
    monkeypatch.setenv("MCP_GITHUB_CLIENT_ID", "example-github-oauth-client-id")
    with pytest.raises(ValueError, match="MCP_GITHUB_CLIENT_ID"):
        create_probe("fixture-only")

    monkeypatch.setenv("MCP_GITHUB_CLIENT_ID", "fixture-client-id")
    monkeypatch.setenv("MCP_PUBLIC_BASE", "https://your-node.ts.net")
    with pytest.raises(ValueError, match="MCP_PUBLIC_BASE"):
        create_probe("fixture-only")
