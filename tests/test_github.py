"""Tests for the first-party GitHub REST provider (github-mcp).

Replaces the dead Smithery-hosted github-mcp remote. A stored GitHub PAT in the
vault drives api.github.com calls through the same audited gateway as every
other provider; the dashboard verify endpoint probes GET /user with it.
"""
import time

import pytest

from app.providers import registry, github
from app.providers.base import ProviderError

TOKEN = "github_pat_ab12cd"
TOKEN_JSON = {"access_token": TOKEN}


# ── registry wiring ─────────────────────────────────────────────────────────

def test_github_registered_and_not_a_remote():
    provider = registry.get_provider("github-mcp")
    assert provider is not None
    assert isinstance(provider, github.GithubProvider)
    assert provider.requires_auth is True
    assert provider.slug == "github-mcp"
    assert all(t["slug"] != "github-mcp" or "hosted" not in t for t in registry.list_public_tools())


# ── validate ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {"tool": "list_repositories", "arguments": {}},
    {"action": "list_repositories"},
    {"list_repositories": {}},
])
def test_validate_accepts_all_shapes(args):
    out = github.provider.validate(args)
    assert out == {"action": "list_repositories", "params": {}}


def test_validate_action_with_params():
    out = github.provider.validate({"action": "search_repositories", "q": "mcp", "per_page": 5})
    assert out["params"]["q"] == "mcp"


def test_validate_bad_types():
    with pytest.raises(ProviderError):
        github.provider.validate("nope")


def test_validate_unknown_action():
    with pytest.raises(ProviderError, match="unknown GitHub action"):
        github.provider.validate({"action": "delete_everything"})


def test_validate_repo_requires_owner_name():
    with pytest.raises(ProviderError, match="owner/name"):
        github.provider.validate({"action": "get_repository", "repo": "norepo"})


def test_validate_search_requires_q():
    with pytest.raises(ProviderError, match="q is required"):
        github.provider.validate({"action": "search_repositories"})


def test_validate_gist_requires_files():
    with pytest.raises(ProviderError, match="files is required"):
        github.provider.validate({"action": "create_gist", "description": "x"})


# ── execute ─────────────────────────────────────────────────────────────────

def test_execute_requires_token():
    out = github.provider.run({"action": "list_repositories"}, "public:read", credential=None)
    assert out["ok"] is False
    assert "no GitHub token" in out["error"]


def test_execute_list_repositories(monkeypatch):
    captured = {}

    def fake_api(method, url, token, params=None, body=None):
        captured.update(method=method, url=url, token=token, params=params)
        return [{"full_name": "a/one", "private": False, "html_url": "https://github.com/a/one",
                 "default_branch": "main", "updated_at": "2026-01-01T00:00:00Z"}]

    monkeypatch.setattr(github, "_api", fake_api)
    out = github.provider.run({"action": "list_repositories"}, "public:read",
                              credential=TOKEN_JSON)
    assert out["ok"] is True
    assert captured["url"] == "https://api.github.com/user/repos"
    assert captured["token"] == TOKEN
    assert captured["params"]["per_page"] == 30
    assert out["result"]["count"] == 1
    assert out["result"]["repositories"][0]["name"] == "a/one"
    assert out["result"]["authenticated_as"].startswith("github")
    assert out["result"]["authenticated_as"].endswith("12cd")


def test_execute_search_repositories(monkeypatch):
    def fake_api(method, url, token, params=None, body=None):
        return {"total_count": 1, "items": [
            {"full_name": "x/y", "html_url": "https://github.com/x/y",
             "stargazers_count": 12, "description": "d", "fork": False, "language": "Python"}]}

    monkeypatch.setattr(github, "_api", fake_api)
    out = github.provider.run({"action": "search_repositories", "q": "mcp"}, "public:read",
                              credential=TOKEN_JSON)
    assert out["ok"] is True
    assert out["result"]["total_count"] == 1
    assert out["result"]["results"][0]["stars"] == 12


def test_execute_get_repository(monkeypatch):
    def fake_api(method, url, token, params=None, body=None):
        return {"full_name": "owner/repo", "description": "r", "stargazers_count": 42,
                "default_branch": "main", "pushed_at": "2026-02-02", "archived": False,
                "license": {"spdx_id": "MIT"}, "html_url": "https://github.com/owner/repo",
                "fork": False}

    monkeypatch.setattr(github, "_api", fake_api)
    out = github.provider.run({"action": "get_repository", "repo": "owner/repo"}, "public:read",
                              credential=TOKEN_JSON)
    assert out["ok"] is True
    assert out["result"]["repository"]["license"] == "MIT"
    assert out["result"]["repository"]["stars"] == 42


def test_execute_star_and_gist(monkeypatch):
    calls = []

    def fake_api(method, url, token, params=None, body=None):
        calls.append(method)
        if method == "PUT":
            return {}
        return {"id": "g1", "html_url": "https://gist.github.com/g1", "public": False}

    monkeypatch.setattr(github, "_api", fake_api)
    out = github.provider.run({"action": "star_repository", "repo": "owner/repo"}, "public:read",
                              credential=TOKEN_JSON)
    assert out["ok"] is True and out["result"]["starred"] is True
    out = github.provider.run(
        {"action": "create_gist", "description": "hi", "files": {"a.txt": "hello"}},
        "public:read", credential=TOKEN_JSON)
    assert out["ok"] is True and out["result"]["id"] == "g1"
    assert calls == ["PUT", "POST"]


def test_execute_github_api_error_surfaced(monkeypatch):
    def fake_api(method, url, token, params=None, body=None):
        raise ProviderError("GitHub API 401: Bad credentials")

    monkeypatch.setattr(github, "_api", fake_api)
    out = github.provider.run({"action": "list_repositories"}, "public:read",
                              credential=TOKEN_JSON)
    assert out["ok"] is False
    assert "401" in out["error"]


def test_execute_scope_gate():
    out = github.provider.run({"action": "list_repositories"}, "commercial:write",
                              credential=TOKEN_JSON)
    assert out["ok"] is False
    assert "not granted" in out["error"]


def test_auth_headers_and_verify_probe(monkeypatch):
    import asyncio
    assert github.GithubProvider._auth_headers(TOKEN_JSON) == {"Authorization": "Bearer github_pat_ab12cd"}
    assert github.GithubProvider._auth_headers({"token": "x"}) == {"Authorization": "Bearer x"}
    assert github.GithubProvider._auth_headers({}) is None
    assert github.GithubProvider._auth_headers(None) is None

    probed = {}

    def fake_api(method, url, token, params=None, body=None):
        probed.update(url=url, token=token)
        return {"login": "octocat"}

    monkeypatch.setattr(github, "_api", fake_api)
    tools = asyncio.run(github.provider._async_list_tools({"Authorization": "Bearer " + TOKEN}))
    names = [t["name"] for t in tools]
    assert "list_repositories" in names and "get_repository" in names
    assert probed["url"].endswith("/user")
    assert probed["token"] == TOKEN


# ── integration: through the app + gateway + vault ─────────────────────────

def _signup(client):
    email = f"github-{int(time.time() * 1e6)}@example.com"
    r = client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    assert r.status_code in (200, 201), r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, email


def test_gateway_execute_github_mcp_allowed(client, monkeypatch):
    def fake_api(method, url, token, params=None, body=None):
        return [{"full_name": "a/one", "private": False}]

    monkeypatch.setattr(github, "_api", fake_api)
    headers, _ = _signup(client)
    r = client.post("/api/v1/connections",
                    json={"provider_slug": "github-mcp", "credential": TOKEN_JSON},
                    headers=headers)
    assert r.status_code == 201, r.text
    r = client.post("/api/v1/execute",
                    json={"provider_slug": "github-mcp",
                          "arguments": {"action": "list_repositories"}},
                    headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["count"] == 1


def test_gateway_execute_github_mcp_no_credential_fails(client):
    headers, _ = _signup(client)
    r = client.post("/api/v1/execute",
                    json={"provider_slug": "github-mcp",
                          "arguments": {"tool": "list_repositories", "arguments": {}}},
                    headers=headers)
    assert r.status_code == 502
    assert "no GitHub token" in r.json()["detail"]


def test_verify_github_mcp_ok(client, monkeypatch):
    def fake_api(method, url, token, params=None, body=None):
        assert token == TOKEN
        return {"login": "octocat"}

    monkeypatch.setattr(github, "_api", fake_api)
    headers, _ = _signup(client)
    r = client.post("/api/v1/connections",
                    json={"provider_slug": "github-mcp", "credential": TOKEN_JSON},
                    headers=headers)
    assert r.status_code == 201
    r = client.get("/api/v1/connections/github-mcp/verify", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["authenticated"] is True
    assert body["tool_count"] == len(github.ACTIONS)
    assert "list_repositories" in body["tools"]


def test_verify_github_mcp_bad_token(client, monkeypatch):
    def fake_api(method, url, token, params=None, body=None):
        raise ProviderError("GitHub API 401: Bad credentials")

    monkeypatch.setattr(github, "_api", fake_api)
    headers, _ = _signup(client)
    r = client.post("/api/v1/connections",
                    json={"provider_slug": "github-mcp", "credential": TOKEN_JSON},
                    headers=headers)
    assert r.status_code == 201
    r = client.get("/api/v1/connections/github-mcp/verify", headers=headers)
    assert r.status_code == 502
    assert "401" in r.json()["detail"]


def test_verify_github_mcp_no_credential_400(client):
    headers, _ = _signup(client)
    r = client.get("/api/v1/connections/github-mcp/verify", headers=headers)
    assert r.status_code == 400
    assert "no credentials" in r.json()["detail"]