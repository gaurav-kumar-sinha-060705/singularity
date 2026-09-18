"""MCP provider-authorization challenge (RFC 9757 popup).

When an authenticated MCP client calls tools/call for a provider that needs a
credential the calling user has not connected, this middleware replies 401 with

    WWW-Authenticate: MCP-Authorization-Request response_type=code \
        resource="notion-mcp" scope="notion-mcp:execute"

instead of letting the tool run/fail. A standards-compliant MCP client (e.g.
Claude) reacts to this by popping a browser window for our authorization server
(issuer = oauth_public_base, metadata at /.well-known/oauth-authorization-server),
where the human signs in and clicks Approve. Our /mcp-auth consent page sees the
`resource` (the provider slug) and on approval writes the provider credential to
the vault and completes the authorization code; on retry the client carries the
same token, the vault now has the credential, and the gateway executes.

Requests that are unauthenticated, not tools/call, or for providers that already
have a connected credential flow straight through unchanged — the SDK's own auth
middleware handles the plain 401 cases.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send

from app.database import SessionLocal
from app.mcp_oauth import SingularityOAuthProvider
from app.models import Connection
from app.providers import registry


class ProviderChallengeMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # main.py runs the underlying Starlette lifespan through this wrapper.
        self.router = getattr(app, "router", None)
        self._provider = SingularityOAuthProvider()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        body = b""
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        provider_slug = await self._needs_challenge(body, scope)
        if provider_slug:
            await self._challenge(send, provider_slug)
            return

        await self.app(scope, _replay_receive(body), send)

    async def _needs_challenge(self, body: bytes, scope: Scope) -> str | None:
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return None
        if payload.get("method") != "tools/call":
            return None
        params = payload.get("params") or {}
        arguments = params.get("arguments") or {}
        slug = arguments.get("provider_slug") or arguments.get("provider")
        if not slug:
            return None

        provider = registry.get_provider(slug)
        if not provider or not getattr(provider, "requires_auth", False):
            return None

        bearer = _bearer_from_scope(scope)
        if not bearer:
            return None
        token = await self._provider.load_access_token(bearer)
        if not token or not token.subject:
            return None

        db = SessionLocal()
        try:
            row = db.query(Connection).filter(
                Connection.user_id == token.subject,
                Connection.provider_slug == slug,
            ).first()
        finally:
            db.close()
        if row:
            return None
        return slug

    async def _challenge(self, send: Send, provider_slug: str) -> None:
        from app.config import get_settings

        base = get_settings().oauth_public_base.rstrip("/")
        body = json.dumps({
            "error": "unauthorized",
            "error_description": (
                f"'{provider_slug}' needs a credential. Open {base}/authorize/{provider_slug}, "
                "sign in, and click Approve, then retry."
            ),
        }).encode()
        www = (
            "MCP-Authorization-Request response_type=code, "
            f'resource="{provider_slug}", scope="{provider_slug}:execute"'
        )
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", www.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def _bearer_from_scope(scope: Scope) -> str | None:
    """Extract 'Bearer <token>' from an ASGI scope's header list."""
    for name, value in scope.get("headers") or []:
        if name.lower() == b"authorization":
            text = value.decode("latin-1")
            if text.lower().startswith("bearer "):
                return text[7:].strip()
            return None
    return None


def _replay_receive(body: bytes):
    """receive callable that yields the buffered body once, then EOF."""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive