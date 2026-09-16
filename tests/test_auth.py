import time

from jose import jwt

from app.config import get_settings
from app.database import SessionLocal
from app.models import User


def _unique_email() -> str:
    return f"user{int(time.time() * 1000000)}@example.com"


def test_signup_and_me(client):
    email = _unique_email()
    resp = client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    assert resp.status_code == 201, resp.text
    tokens = resp.json()
    assert tokens["access_token"] and tokens["refresh_token"]

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == email


def test_signup_duplicate_email(client):
    email = _unique_email()
    client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    resp = client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    assert resp.status_code == 409


def test_signup_rejects_bad_email(client):
    resp = client.post("/api/v1/auth/signup", json={"email": "not-an-email", "password": "hunter2hunter"})
    assert resp.status_code == 422


def test_signin_wrong_password(client):
    email = _unique_email()
    client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    resp = client.post("/api/v1/auth/signin", json={"email": email, "password": "wrongpass"})
    assert resp.status_code == 401


def test_signin_ok(client):
    email = _unique_email()
    client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    resp = client.post("/api/v1/auth/signin", json={"email": email, "password": "hunter2hunter"})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_me_requires_token(client):
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_rejects_refresh_token_as_access(client):
    email = _unique_email()
    tokens = client.post("/api/v1/auth/signup",
                         json={"email": email, "password": "hunter2hunter"}).json()
    resp = client.get("/api/v1/auth/me",
                      headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
    assert resp.status_code == 401


def test_refresh_issues_new_access(client):
    email = _unique_email()
    tokens = client.post("/api/v1/auth/signup",
                         json={"email": email, "password": "hunter2hunter"}).json()
    resp = client.post("/api/v1/auth/refresh",
                       headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_password_is_hashed_not_plaintext(client):
    email = _unique_email()
    client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        assert user is not None
        assert user.password_hash != "hunter2hunter"
        assert user.password_hash.startswith("$2")
        assert user.check_password("hunter2hunter")
        assert not user.check_password("nope")
    finally:
        db.close()


def test_expired_token_rejected(client):
    db = SessionLocal()
    try:
        email = _unique_email()
        user = User(id=__import__("app.models", fromlist=["_user_id"])._user_id(email), email=email)
        user.set_password("hunter2hunter")
        db.add(user)
        db.commit()
        expired = jwt.encode(
            {"sub": user.id, "email": email, "type": "access",
             "iat": 0, "exp": int(time.time()) - 10},
            get_settings().jwt_secret, algorithm="HS256",
        )
    finally:
        db.close()
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401