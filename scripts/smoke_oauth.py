"""Full OAuth handshake smoke test against a deployed Singularity (streamable HTTP).

Drives RFC 9745: fetch metadata -> dynamic register -> /authorize -> consent
signup -> approve -> /token -> authenticated initialize + tools/list + call.

Usage:
    python scripts/smoke_oauth.py https://singularity-osd2.onrender.com
"""

import base64
import hashlib
import secrets
import sys
import urllib.parse

import httpx

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "https://singularity-osd2.onrender.com"


def pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def main() -> None:
    with httpx.Client(base_url=BASE, follow_redirects=False, timeout=30.0) as c:
        # 1) metadata
        meta = c.get("/.well-known/oauth-authorization-server").json()
        print(f"issuer: {meta['issuer']}")
        assert meta["issuer"] == BASE, "issuer mismatch (RENDER_EXTERNAL_URL not picked up?)"

        # 2) DCR (public client, defaults flow-compatible)
        redirect_uri = f"{BASE}/callback"
        reg = c.post(
            "/register",
            json={
                "redirect_uris": [redirect_uri],
                "client_name": "smoke-oauth",
                "token_endpoint_auth_method": "none",
            },
        )
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]
        print(f"registered client {client_id}")

        # 3) authorize
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(16)
        auth = c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "scope": "mcp:tools",
            },
        )
        assert auth.status_code == 302, auth.text
        consent_url = auth.headers["location"]
        print(f"authorize -> {consent_url}")

        # 4) consent page
        page = c.get(consent_url)
        assert page.status_code == 200
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(consent_url).query)
        form = {k: v[0] for k, v in qs.items()}

        # 5) sign up (fresh user)
        email = f"smoke-{secrets.token_hex(6)}@example.com"
        password = "smoke-oAuth-Pass1!"
        form.update({"signup": "1", "email": email, "password": password})
        resp = c.post("/mcp-auth/consent", data=form)
        if resp.status_code == 409:
            form["signup"] = "0"
            resp = c.post("/mcp-auth/consent", data=form)
        assert resp.status_code == 200, resp.text
        print(f"user {email} signed up")

        # 6) approve -> code in redirect
        form["approve"] = "1"
        resp = c.post("/mcp-auth/consent", data=form)
        assert resp.status_code == 303, resp.text
        code = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)["code"][0]
        print(f"authorization code issued")

        # 7) token
        tok = c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert tok.status_code == 200, tok.text
        access_token = tok.json()["access_token"]
        print(f"access token issued (type {tok.json().get('token_type')})")

        # 8) authenticated /mcp
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

        init = c.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke-oauth", "version": "0"},
                },
            },
        )
        assert init.status_code == 200, init.text
        si = init.json()["result"]["serverInfo"]
        print(f"MCP server: {si['name']} {si['version']}")

        tools = c.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        names = [t["name"] for t in tools.json()["result"]["tools"]]
        print(f"tools listed: {len(names)} -> {', '.join(names[:5])}...")

        print("OK — full OAuth handshake succeeded")


if __name__ == "__main__":
    main()