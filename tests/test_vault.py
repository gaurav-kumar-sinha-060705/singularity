import json
import time

from app.database import SessionLocal
from app.models import Connection
from app.vault import decrypt, encrypt


def _auth_headers(client) -> dict:
    email = f"vault{int(time.time() * 1000000)}@example.com"
    tokens = client.post("/api/v1/auth/signup",
                         json={"email": email, "password": "hunter2hunter"}).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_vault_roundtrip():
    secret = json.dumps({"api_key": "sk_test_abc"})
    token = encrypt(secret)
    assert token != secret
    assert decrypt(token) == secret


def test_vault_ciphertext_differs_each_time():
    assert encrypt("x") != encrypt("x")


def test_connection_requires_auth(client):
    resp = client.post("/api/v1/connections",
                       json={"provider_slug": "stripe-mcp", "credential": {"api_key": "sk"}})
    assert resp.status_code == 401


def test_connection_store_and_list(client):
    headers = _auth_headers(client)
    resp = client.post("/api/v1/connections",
                       json={"provider_slug": "stripe-mcp", "credential": {"api_key": "sk_test_abc"}},
                       headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["provider_slug"] == "stripe-mcp"
    assert "credential" not in body  # never returned

    listed = client.get("/api/v1/connections", headers=headers)
    assert listed.status_code == 200
    slugs = [c["provider_slug"] for c in listed.json()]
    assert "stripe-mcp" in slugs


def test_connection_stored_encrypted(client):
    headers = _auth_headers(client)
    client.post("/api/v1/connections",
                json={"provider_slug": "stripe-mcp", "credential": {"api_key": "sk_live_secret"}},
                headers=headers)
    db = SessionLocal()
    try:
        rows = db.query(Connection).all()
        assert rows
        for row in rows:
            assert "sk_live_secret" not in row.credential_json
    finally:
        db.close()


def test_connection_upsert_updates(client):
    headers = _auth_headers(client)
    first = client.post("/api/v1/connections",
                        json={"provider_slug": "stripe-mcp", "credential": {"api_key": "a"}},
                        headers=headers).json()
    second = client.post("/api/v1/connections",
                         json={"provider_slug": "stripe-mcp", "credential": {"api_key": "b"}},
                         headers=headers).json()
    assert first["id"] == second["id"]


def test_connection_delete(client):
    headers = _auth_headers(client)
    created = client.post("/api/v1/connections",
                          json={"provider_slug": "stripe-mcp", "credential": {"api_key": "a"}},
                          headers=headers).json()
    resp = client.delete(f"/api/v1/connections/{created['id']}", headers=headers)
    assert resp.status_code == 204
    listed = client.get("/api/v1/connections", headers=headers).json()
    assert created["id"] not in [c["id"] for c in listed]


def test_connection_cannot_delete_others(client):
    headers_a = _auth_headers(client)
    headers_b = _auth_headers(client)
    created = client.post("/api/v1/connections",
                          json={"provider_slug": "stripe-mcp", "credential": {"api_key": "a"}},
                          headers=headers_a).json()
    resp = client.delete(f"/api/v1/connections/{created['id']}", headers=headers_b)
    assert resp.status_code == 404