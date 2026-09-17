"""private_key_jwt support for the MCP SDK auth server.

The SDK's bundled registration / client-authentication handlers only accept
`client_secret_post` / `client_secret_basic` / `none`. Claude.ai dynamic client
registration sends `token_endpoint_auth_method: private_key_jwt` (RFC 7591),
which the stock `RegistrationHandler` hard-refuses — so a Claude connector can
never complete onboarding. This module provides drop-in replacements:

  - `PrivateKeyJwtRegistrationHandler`: accepts private_key_jwt, mints no
    client secret, and persists the client's public JWKS for later use.
  - `PrivateKeyJwtClientAuthenticator`: authenticates /token requests that
    present an RFC 7523 `client_assertion` signed with the client's registered
    JWKS (iss/sub == client_id, aud == this server, exp enforced).
  - `install_oauth_extensions()`: swaps these into `mcp.server.auth.routes`
    and advertises private_key_jwt in RFC 8414 metadata. MUST run before the
    streamable-HTTP app is built (see `build_mcp_asgi_app`).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import uuid4

from jose import jwt as jose_jwt
from jose.exceptions import JWTError, JWSError
from pydantic import BaseModel, ValidationError
from starlette.responses import Response

from mcp.server.auth.errors import stringify_pydantic_error
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import JWT_BEARER_GRANT_TYPE, OAuthClientInformationFull, OAuthClientMetadata

from app.config import get_settings

_extensions_installed = False

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class _RegistrationErrorResponse(BaseModel):
    error: str
    error_description: str | None = None

    model_config = {"extra": "forbid"}

# Algorithms we will verify client_assertion signatures with.
SUPPORTED_ALGS = [
    "RS256", "RS384", "RS512",
    "ES256", "ES384", "ES512",
    "PS256", "PS384", "PS512",
    "EdDSA",
]


def _pick_jwk(jwks: dict, kid: str | None) -> dict | None:
    """Select the verification key (single JWK or a {"keys": [...]} JWK set)."""
    if not jwks:
        return None
    if isinstance(jwks, dict) and isinstance(jwks.get("keys"), list):
        keys = jwks["keys"]
        if kid:
            for key in keys:
                if key.get("kid") == kid:
                    return key
        return keys[0] if keys else None
    return jwks


def _audience_candidates() -> list[str]:
    """Token endpoint + issuer base — RFC 7523 §3 permits either in `aud`."""
    base = get_settings().oauth_public_base.rstrip("/")
    return [f"{base}/token", base]


def _verify_client_assertion(assertion: str, jwks: dict, client_id: str) -> None:
    """Validate an RFC 7523 JWT client assertion against the client's JWKS."""
    try:
        header = jose_jwt.get_unverified_header(assertion)
    except (JWTError, JWSError) as exc:
        raise AuthenticationError(f"Invalid client_assertion header: {exc}") from exc

    alg = header.get("alg")
    if alg not in SUPPORTED_ALGS:
        raise AuthenticationError(f"Unsupported client_assertion algorithm: {alg}")
    key = _pick_jwk(jwks, header.get("kid"))
    if not key:
        raise AuthenticationError("Client has no verification key registered (jwks)")

    options = {
        "verify_signature": True,
        "verify_exp": True,
        "verify_nbf": True,
        "verify_iat": False,  # RFC 7523 does not mandate iat; some clients omit it
        "verify_aud": False,  # checked manually across accepted audiences
        "verify_iss": False,  # checked manually below
        "verify_sub": False,  # checked manually below
    }
    try:
        claims = jose_jwt.decode(assertion, key, algorithms=[alg], options=options)
    except (JWTError, JWSError, ValueError) as exc:
        raise AuthenticationError(f"Invalid client_assertion: {exc}") from exc

    if claims.get("iss") != client_id or claims.get("sub") != client_id:
        raise AuthenticationError("client_assertion iss/sub must equal client_id")
    if claims.get("aud") not in _audience_candidates():
        raise AuthenticationError("client_assertion aud does not match this authorization server")
    if not claims.get("jti"):
        raise AuthenticationError("client_assertion requires a jti claim")


class PrivateKeyJwtClientAuthenticator(ClientAuthenticator):
    """ClientAuthenticator that also verifies RFC 7523 private_key_jwt assertions."""

    async def authenticate_request(self, request) -> OAuthClientInformationFull:
        form_data = await request.form()
        client_id = form_data.get("client_id")
        if not client_id:
            raise AuthenticationError("Missing client_id")

        client = await self.provider.get_client(str(client_id))
        if not client:
            raise AuthenticationError("Invalid client_id")

        if client.token_endpoint_auth_method == "private_key_jwt":
            if form_data.get("client_assertion_type") != CLIENT_ASSERTION_TYPE:
                raise AuthenticationError(
                    "client_assertion_type must be "
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                )
            assertion = form_data.get("client_assertion")
            if not assertion:
                raise AuthenticationError("Missing client_assertion")
            _verify_client_assertion(str(assertion), client.jwks or {}, client.client_id)
            return client

        return await self._authenticate_standard(request, client, form_data)

    async def _authenticate_standard(self, request, client, form_data) -> OAuthClientInformationFull:
        """The stock handler's secret-based validation paths (basic / post / none)."""
        from base64 import b64decode
        from binascii import Error as BinasciiError
        from urllib.parse import unquote

        request_client_secret: str | None = None
        auth_header = request.headers.get("Authorization", "")

        method = client.token_endpoint_auth_method
        if method == "client_secret_basic":
            if not auth_header.startswith("Basic "):
                raise AuthenticationError("Missing or invalid Basic authentication in Authorization header")
            try:
                encoded = auth_header[6:]
                decoded = b64decode(encoded).decode("utf-8")
                if ":" not in decoded:
                    raise ValueError("Invalid Basic auth format")
                basic_client_id, request_client_secret = decoded.split(":", 1)
                basic_client_id = unquote(basic_client_id)
                request_client_secret = unquote(request_client_secret)
                if basic_client_id != client.client_id:
                    raise AuthenticationError("Client ID mismatch in Basic auth")
            except (ValueError, UnicodeDecodeError, BinasciiError):
                raise AuthenticationError("Invalid Basic authentication header")
        elif method == "client_secret_post":
            raw = form_data.get("client_secret")
            if isinstance(raw, str):
                request_client_secret = str(raw)
        elif method == "none":
            request_client_secret = None
        else:
            raise AuthenticationError(f"Unsupported auth method: {method}")

        if method != "none" and not client.client_secret:
            raise AuthenticationError("Client is registered for secret-based authentication but has no stored secret")

        if client.client_secret:
            if not request_client_secret:
                raise AuthenticationError("Client secret is required")
            import hmac

            if not hmac.compare_digest(client.client_secret.encode(), request_client_secret.encode()):
                raise AuthenticationError("Invalid client_secret")
            if client.client_secret_expires_at and client.client_secret_expires_at < int(time.time()):
                raise AuthenticationError("Client secret has expired")

        return client


class PrivateKeyJwtRegistrationHandler(RegistrationHandler):
    """Registration handler that allows private_key_jwt clients (RFC 7591).

    Mirrors the stock handler's validation (scopes, grant/response types) but:
      - does not hard-refuse private_key_jwt;
      - mints a client secret only for secret-based methods;
      - accepts a rejection of the identity-assertion grant via DCR.
    """

    async def handle(self, request) -> Response:
        try:
            body = await request.body()
            client_metadata = OAuthClientMetadata.model_validate_json(body)
        except ValidationError as validation_error:
            return PydanticJSONResponse(
                content=_RegistrationErrorResponse(
                    error="invalid_client_metadata",
                    error_description=stringify_pydantic_error(validation_error),
                ),
                status_code=400,
            )

        client_id = str(uuid4())

        method = client_metadata.token_endpoint_auth_method or "client_secret_post"
        if method in ("private_key_jwt", "none", "client_secret_post", "client_secret_basic"):
            pass
        else:
            return PydanticJSONResponse(
                content=_RegistrationErrorResponse(
                    error="invalid_client_metadata",
                    error_description=f"token_endpoint_auth_method '{method}' is not supported",
                ),
                status_code=400,
            )

        client_secret = None
        if method in ("client_secret_post", "client_secret_basic"):
            import secrets

            client_secret = secrets.token_hex(32)

        if client_metadata.scope is None and self.options.default_scopes is not None:
            client_metadata.scope = " ".join(self.options.default_scopes)
        elif client_metadata.scope is not None and self.options.valid_scopes is not None:
            requested_scopes = set(client_metadata.scope.split())
            valid_scopes = set(self.options.valid_scopes)
            if not requested_scopes.issubset(valid_scopes):
                return PydanticJSONResponse(
                    content=_RegistrationErrorResponse(
                        error="invalid_client_metadata",
                        error_description="Requested scopes are not valid: "
                        + ", ".join(sorted(requested_scopes - valid_scopes)),
                    ),
                    status_code=400,
                )
        if "authorization_code" not in client_metadata.grant_types:
            return PydanticJSONResponse(
                content=_RegistrationErrorResponse(
                    error="invalid_client_metadata",
                    error_description="grant_types must include 'authorization_code'",
                ),
                status_code=400,
            )
        if JWT_BEARER_GRANT_TYPE in client_metadata.grant_types:
            return PydanticJSONResponse(
                content=_RegistrationErrorResponse(
                    error="invalid_client_metadata",
                    error_description=(
                        f"grant_types must not include '{JWT_BEARER_GRANT_TYPE}'; "
                        "the identity-assertion grant requires a pre-registered client"
                    ),
                ),
                status_code=400,
            )
        if "code" not in client_metadata.response_types:
            return PydanticJSONResponse(
                content=_RegistrationErrorResponse(
                    error="invalid_client_metadata",
                    error_description="response_types must include 'code' for authorization_code grant",
                ),
                status_code=400,
            )

        now = datetime.now(timezone.utc)
        client_secret_expires_at = None
        if client_secret is not None:
            client_secret_expires_at = (
                int(now.timestamp()) + self.options.client_secret_expiry_seconds
                if self.options.client_secret_expiry_seconds is not None
                else 0
            )

        client_info = OAuthClientInformationFull.model_validate(
            {
                **client_metadata.model_dump(),
                "client_id": client_id,
                "client_id_issued_at": int(now.timestamp()),
                "client_secret": client_secret,
                "client_secret_expires_at": client_secret_expires_at,
            }
        )
        try:
            await self.provider.register_client(client_info)
            return PydanticJSONResponse(content=client_info, status_code=201)
        except RegistrationError as e:
            return PydanticJSONResponse(
                content=_RegistrationErrorResponse(error=e.error, error_description=e.error_description),
                status_code=400,
            )


def install_oauth_extensions() -> None:
    """Swap the extended handlers into the SDK before the /mcp app is built.

    `create_auth_routes` resolves these names from `mcp.server.auth.routes`
    globals at app-build time, so patching the module attributes — done once,
    before `MCPServer.streamable_http_app()` — is sufficient.
    """
    global _extensions_installed
    if _extensions_installed:
        return
    import mcp.server.auth.routes as routes

    routes.RegistrationHandler = PrivateKeyJwtRegistrationHandler
    routes.ClientAuthenticator = PrivateKeyJwtClientAuthenticator

    _orig_build_metadata = routes.build_metadata

    def _patched_build_metadata(*args, **kwargs):
        metadata = _orig_build_metadata(*args, **kwargs)
        supported = list(metadata.token_endpoint_auth_methods_supported or [])
        if "private_key_jwt" not in supported:
            supported.append("private_key_jwt")
        metadata.token_endpoint_auth_methods_supported = supported
        if metadata.revocation_endpoint_auth_methods_supported is not None:
            revocation_supported = list(metadata.revocation_endpoint_auth_methods_supported)
            if "private_key_jwt" not in revocation_supported:
                revocation_supported.append("private_key_jwt")
            metadata.revocation_endpoint_auth_methods_supported = revocation_supported
        return metadata

    routes.build_metadata = _patched_build_metadata
    _extensions_installed = True