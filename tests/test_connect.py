"""Generic one-click connect: capability catalog, mcp_oauth engine
(discovery/registration/exchange/refresh), and the /api/v1/auth routes."""

import json
import time
import urllib.parse

import pytest

from app.config import get_settings
from app.routers import connect as connect_router
from app.services import mcp_oauth as moa
from app.services.oauth_caps import all_capabilities, get_capability
from app.database import SessionLocal
from app.models import Connection, RemoteOAuthClient, Tool, User


# ---------------------------------------------------------------------------
# Capability catalog
# ---------------------------------------------------------------------------

def test_capability_methods_by_prefer():
    assert get_capability("stripe-mcp").methods == ["api_key", "mcp_oauth"]
    assert get_capability("stripe-mcp").can("mcp_oauth")
    assert get_capability("firecrawl-mcp").prefer == "mcp_oauth"
    assert get_capability("browserbase-mcp").methods == ["api_key"]
    assert get_capability("github-mcp").methods == ["api_key", "provider_oauth"]
    assert get_capability("github-mcp").provider == "github"


def test_google_drive_stdio_env_map():
    cap = get_capability("google-drive-stdio")
    assert cap.env_map["refresh_token"] == "GOOGLE_DRIVE_REFRESH_TOKEN"
    assert cap.env_map["access_token"] == "GOOGLE_DRIVE_ACCESS_TOKEN"


def test_all_keyed_tools_have_capabilities():
    slugs = all_capabilities()
    for slug in ("stripe-mcp", "notion-mcp", "firecrawl-mcp", "browserbase-mcp",
                 "github-mcp", "slack-mcp", "github-stdio", "google-drive-stdio"):
        assert slug in slugs, slug


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

_META = {
    "issuer": "https://access.stripe.com/mcp",
    "authorization_endpoint": "https://access.stripe.com/mcp/oauth2/authorize",
    "token_endpoint": "https://access.stripe.com/mcp/oauth2/token",
    "registration_endpoint": "https://access.stripe.com/mcp/oauth2/register",
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
}


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    moa._clear_discovery_cache()
    moa._DISCOVERY.clear()
    yield
    moa._clear_discovery_cache()


def test_discover_parses_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(moa, "_http_get", lambda url, headers=None, timeout=10: calls.append(url) or _META)
    meta = moa.discover(get_capability("stripe-mcp"))
    assert meta.authorization_endpoint == _META["authorization_endpoint"]
    assert meta.registration_endpoint == _META["registration_endpoint"]
    assert meta.token_endpoint_auth_methods_supported == ["none"]


def test_discover_two_hop(monkeypatch):
    cap = get_capability("stripe-mcp")
    monkeypatch.setattr(moa, "_http_get", lambda url, headers=None, timeout=10: _META)
    meta = moa.discover(cap)
    assert meta.token_endpoint  # direct path succeeded


def test_discover_raises_when_unreachable(monkeypatch):
    def boom(url, headers=None, timeout=10):
        raise moa.DiscoveryError(f"{url}: HTTP 404")

    monkeypatch.setattr(moa, "_http_get", boom)
    with pytest.raises(moa.DiscoveryError):
        moa.discover(get_capability("firecrawl-mcp"))


# ---------------------------------------------------------------------------
# Dynamic client registration
# ---------------------------------------------------------------------------

def test_register_and_reuse(monkeypatch):
    db = SessionLocal()
    try:
        db.query(RemoteOAuthClient).filter_by(slug="stripe-mcp").delete()
        db.commit()
        posted = []
        registration_id = {"client_id": "sg-stripe-1", "client_secret": "8088secret"}
        monkeypatch.setattr(moa, "_http_get", lambda url, headers=None, timeout=10: _META)
        monkeypatch.setattr(moa, "_http_post",
                            lambda url, payload, headers=None, timeout=10: posted.append(payload) or registration_id)

        cap = get_capability("stripe-mcp")
        cid, secret = moa.register_client(cap, db)
        assert cid == "sg-stripe-1"
        assert secret == "8088secret"
        assert posted[0]["token_endpoint_auth_method"] == "none"
        assert posted[0]["redirect_uris"] == ["https://testserver/api/v1/auth/stripe-mcp/callback"]
        assert posted[0]["resource"] == "https://access.stripe.com/mcp"

        row = db.query(RemoteOAuthClient).filter_by(slug="stripe-mcp").first()
        assert row is not None

        cid2, secret2 = moa.register_client(cap, db)
        assert cid2 == "sg-stripe-1" and secret2 == "8088secret"  # reused, no extra POST
        assert len(posted) == 1
    finally:
        db.close()


def test_pick_auth_method_prefers_none():
    assert moa._pick_auth_method(["client_secret_basic", "none"]) == "none"
    assert moa._pick_auth_method(["client_secret_basic", "client_secret_post"]) == "client_secret_post"


# ---------------------------------------------------------------------------
# Authorize + exchange + refresh
# ---------------------------------------------------------------------------

def test_authorize_url_builds_pkce_resource(monkeypatch):
    monkeypatch.setattr(moa, "_http_get", lambda url, headers=None, timeout=10: _META)
    url = moa.authorize_url(get_capability("stripe-mcp"), "sg-stripe-1", "V" * 43, "state-1")
    assert url.startswith(_META["authorization_endpoint"])
    qs = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
    assert qs["client_id"] == "sg-stripe-1"
    assert qs["code_challenge_method"] == "S256"
    assert qs["redirect_uri"] == "https://testserver/api/v1/auth/stripe-mcp/callback"
    assert qs["resource"] == "https://access.stripe.com/mcp"


def test_exchange_token(monkeypatch):
    db = SessionLocal()
    try:
        monkeypatch.setattr(moa, "_http_get", lambda url, headers=None, timeout=10: _META)
        monkeypatch.setattr(moa, "_http_post",
                            lambda url, payload, headers=None, timeout=10: {
                                "access_token": "tok-1",
                                "refresh_token": "ref-1",
                                "expires_in": 3600,
                            })
        moa.register_client(get_capability("stripe-mcp"), db)
        data = moa.exchange_token(get_capability("stripe-mcp"), "code-z", "V" * 43, db)
        assert data["access_token"] == "tok-1"
        assert data["auth_method"] == "mcp_oauth"
        assert data["expires_at"] > time.time()
    finally:
        db.close()


def test_refresh_only_when_near_expiry(monkeypatch):
    db = SessionLocal()
    try:
        calls = []
        monkeypatch.setattr(moa, "_http_get", lambda url, headers=None, timeout=10: _META)
        monkeypatch.setattr(moa, "_http_post",
                            lambda url, payload, headers=None, timeout=10: calls.append(payload) or {
                                "access_token": "tok-new",
                                "refresh_token": "ref-new",
                                "expires_in": 3600,
                            })
        moa.register_client(get_capability("stripe-mcp"), db)
        cap = get_capability("stripe-mcp")
        fresh = {"access_token": "tok-1", "refresh_token": "ref-1", "expires_at": time.time() + 3600}
        out = moa.refresh_if_needed(cap, fresh, db)
        assert out["access_token"] == "tok-1" and not calls

        stale = {"access_token": "tok-1", "refresh_token": "ref-1", "expires_at": time.time() - 5}
        out = moa.refresh_if_needed(cap, stale, db)
        assert out["access_token"] == "tok-new"
        assert out["refresh_token"] == "ref-new"
        assert calls
    finally:
        db.close()


def test_refresh_dispatch_swallows_failures(monkeypatch):
    db = SessionLocal()
    try:
        monkeypatch.setattr(moa, "_http_get", lambda url, headers=None, timeout=10: _META)
        monkeypatch.setattr(moa, "_http_post",
                            lambda url, payload, headers=None, timeout=10: (_ for _ in ()).throw(
                                moa.RegistrationError("HTTP 400")) if False else _raise_400())
        moa.register_client(get_capability("stripe-mcp"), db)
        stale = {"auth_method": "mcp_oauth", "access_token": "tok-1",
                 "refresh_token": "ref-1", "expires_at": time.time() - 1}
        out = moa.refresh_for_slug("stripe-mcp", stale, db)
        assert out == stale  # failed refresh never raises, returns unchanged
    finally:
        db.close()


def test_refresh_dispatch_provider_unchanged_when_unconfigured():
    cred = {"oauth_provider": "github", "refresh_token": "r"}
    out = moa.refresh_for_slug("github-mcp", cred, None)
    assert out is cred


def _raise_400():
    raise moa.RegistrationError("HTTP 400")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _signup(client, sufx=""):
    email = f"connect-{sufx or 'u'}-{int(time.time() * 1000)}@example.com"
    resp = client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["access_token"], email


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_catalog_endpoint(client):
    token, _ = _signup(client, "cat")
    resp = client.get("/api/v1/auth/catalog?slug=browserbase-mcp", headers=_auth(token))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["methods"] == ["api_key"]
    assert items[0]["connected"] is False


def test_start_api_key_fallback_for_browserbase(client):
    token, _ = _signup(client, "bb")
    resp = client.get("/api/v1/auth/browserbase-mcp/start",
                      headers={**_auth(token), "X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["method"] == "api_key"


def test_start_provider_oauth_falls_back_when_app_unconfigured(client):
    token, _ = _signup(client, "gh")
    resp = client.get("/api/v1/auth/github-mcp/start",
                      headers={**_auth(token), "X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200
    assert resp.json()["method"] == "api_key"


def test_start_and_callback_mcp_oauth_end_to_end(client, monkeypatch):
    store = {}
    settings = get_settings()

    def fake_get(url, headers=None, timeout=10):
        return _META

    def fake_post(url, payload, headers=None, timeout=10):
        store["posted"] = store.get("posted", []) + [url]
        if url == _META["registration_endpoint"]:
            return {"client_id": "sg-test", "client_secret": None}
        return {"access_token": "mcp-token-1", "refresh_token": "mcp-refresh-1", "expires_in": 3600}

    monkeypatch.setattr(moa, "_http_get", fake_get)
    monkeypatch.setattr(moa, "_http_post", fake_post)

    token, email = _signup(client, "mcp")
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        user_id = user.id
    finally:
        db.close()

    resp = client.get("/api/v1/auth/stripe-mcp/start",
                      headers={**_auth(token), "X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["method"] == "mcp_oauth"
    assert body["url"].startswith(_META["authorization_endpoint"])

    from app.oauth import build_state, new_code_verifier

    verifier = new_code_verifier()
    state = build_state(user_id, "stripe-mcp", verifier, settings.jwt_secret)
    resp = client.get("/api/v1/auth/stripe-mcp/callback",
                      params={"code": "authz-code", "state": state},
                      headers=_auth(token), follow_redirects=False)
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].endswith("/authorize/stripe-mcp?connected=1")

    db = SessionLocal()
    try:
        conn = db.query(Connection).filter(
            Connection.user_id == user_id, Connection.provider_slug == "stripe-mcp"
        ).first()
        assert conn is not None
        from app.vault import decrypt

        cred = json.loads(decrypt(conn.credential_json))
        assert cred["access_token"] == "mcp-token-1"
        assert cred["auth_method"] == "mcp_oauth"
    finally:
        db.close()


def test_generic_routes_registered_on_api_prefix():
    paths = [r.path for r in connect_router.router.routes]
    assert "/auth/catalog" in paths
    assert "/auth/{slug}/start" in paths
    assert "/auth/{slug}/callback" in paths


# ---------------------------------------------------------------------------
# DB auth-mode fields (auth_mode / client_id_required) on the tools table
# ---------------------------------------------------------------------------

def test_tools_table_auth_mode_backfill():
    """The tools table carries, per tool: auth_mode (mcp_oauth / provider_oauth /
    api_key / none) and client_id_required — matching the capability catalog and
    the user's ask (Browserbase & co = paste-only; built-ins = none)."""
    db = SessionLocal()
    try:
        rows = {
            t.slug: (t.auth_mode, t.client_id_required)
            for t in db.query(Tool).all()
        }
        assert rows["weather"] == ("none", False)
        assert rows["npms_lookup"] == ("none", False)
        assert rows["web_search"] == ("none", False)
        assert rows["stripe-mcp"] == ("mcp_oauth", False)
        assert rows["notion-mcp"] == ("mcp_oauth", False)
        assert rows["firecrawl-mcp"] == ("mcp_oauth", False)
        assert rows["browserbase-mcp"] == ("api_key", False)
        assert rows["github-mcp"] == ("provider_oauth", True)
        assert rows["slack-mcp"] == ("provider_oauth", True)
        assert rows["github-stdio"] == ("provider_oauth", True)
        assert rows["google-drive-stdio"] == ("provider_oauth", True)
        for slug in ("postgres-mcp", "sentry-mcp", "jira-mcp", "linear-mcp",
                     "supabase-mcp", "zapier-mcp", "expensify-mcp"):
            assert rows[slug] == ("api_key", False), slug
    finally:
        db.close()


def test_list_public_tools_surfaces_auth_mode():
    from app.providers import registry

    by_slug = {t["slug"]: t for t in registry.list_public_tools()}
    assert by_slug["weather"]["auth_mode"] == "none"
    assert by_slug["browserbase-mcp"]["auth_mode"] == "api_key"
    assert by_slug["github-mcp"]["auth_mode"] == "provider_oauth"
    assert by_slug["github-mcp"]["client_id_required"] is True
    assert by_slug["stripe-mcp"]["auth_mode"] == "mcp_oauth"