"""MCP OAuth consent + login pages (RFC 9745 human-facing step).

The SDK's /authorize handler validates the client + PKCE params, then calls
`provider.authorize()` which redirects the human's browser here. The human
signs in (or creates an account), approves the scopes, and we mint a one-time
authorization code that Claude exchanges at /token — without ever handling the
client-secret or the code_verifier (the SDK does that server-side).

The signed `mcp_session` cookie carries `user_id` so a logged-in human does not
re-enter credentials on every connector connect.
"""

from __future__ import annotations

import json
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.mcp_oauth import get_oauth_user_by_email, issue_authorization_code
from app.mcp_session import (
    read_session,
    set_session_cookie,
    wrap_pending_cookie,
)
from app.models import Connection, OAuthClient, User, log_event
from app.oauth import get_provider
from app.vault import encrypt

router = APIRouter(prefix="/mcp-auth", tags=["mcp-auth"])

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Singularity</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, sans-serif; max-width: 460px; margin: 5vh auto; padding: 0 20px; line-height: 1.5; }}
h1 {{ font-size: 1.4rem; }}
.box {{ border: 1px solid #8884; border-radius: 12px; padding: 20px 24px; }}
label {{ display: block; margin: 12px 0 4px; font-weight: 600; font-size: .85rem; }}
input {{ width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px; border: 1px solid #8888; font-size: 1rem; }}
button {{ margin-top: 18px; width: 100%; padding: 11px; border: 0; border-radius: 8px; background: #0b58fe; color: #fff; font-size: 1rem; font-weight: 600; cursor: pointer; }}
button:hover {{ background: #0a4ddb; }}
.alt {{ text-align: center; margin-top: 16px; font-size: .9rem; }}
.hint {{ margin-top: 16px; font-size: .8rem; opacity: .7; }}
a {{ color: #0b58fe; }}
</style></head><body>
{body}
</body></html>"""

_FORM = """
<div class="box">
<h1>Sign in to connect</h1>
<p>Connect this client to Singularity. Your credentials only identify you — provider
API keys stay encrypted in the vault.</p>
<form method="post" action="/mcp-auth/consent">
  <input type="hidden" name="signup" value="0">
  {hidden}
  <label for="email">Email</label>
  <input type="email" id="email" name="email" required autocomplete="email">
  <label for="password">Password</label>
  <input type="password" id="password" name="password" required autocomplete="current-password">
  <button type="submit">Sign in</button>
</form>
<p class="alt">First time? <button type="button" onclick="toggleSignup()">Create an account</button></p>
</div>
<script>
function toggleSignup() {{
  var f = document.querySelector('form');
  f.action = '/mcp-auth/consent?signup=1';
  f.querySelector('input[name=signup]').value = '1';
  f.querySelector('button[type=submit]').textContent = 'Create account';
}}
</script>
"""

_CONSENT = """
<div class="box">
<h1>Connect {client_name}</h1>
<p>The client <strong>{client_name}</strong> requests access to your Singularity
tools on your behalf.</p>
<p>Scopes requested: <strong>{scopes}</strong></p>
<form method="post" action="/mcp-auth/consent">
  {hidden}
  <button type="submit" name="approve" value="1">Approve</button>
</form>
</div>
"""

_PROVIDER_CONNECT = """
<div class="box">
<h1>Connect {provider_name}</h1>
<p>To run <strong>{provider_name}</strong> tools through Singularity, the gateway
needs a credential for {resource}.</p>
{connect_ui}
<p class="hint">Credentials are encrypted in the vault on this Singularity
instance and are only ever used through the audited gateway.</p>
</div>
"""

_PROVIDER_APPROVE = """
<form method="post" action="/mcp-auth/consent">
  {hidden}
  <button type="submit" name="approve" value="1">Approve &amp; connect {provider_name}</button>
</form>
<p class="alt">You'll be taken to {provider_name} to approve, then dropped back here automatically.</p>
"""

_PROVIDER_TOKEN = """
<form method="post" action="/mcp-auth/consent">
  {hidden}
  <label for="token">Credential ({hint})</label>
  <input type="password" id="token" name="token" autocomplete="off" placeholder="{hint}">
  {error}
  <button type="submit" name="approve" value="1">Connect (store credential)</button>
</form>
<p class="alt">Setting up {provider_name} OAuth later turns this into a one-click Approve.</p>
"""


def _escape(value) -> str:
    from html import escape

    return escape(str(value), quote=True)


def _provider_hint(slug: str) -> str:
    from app.providers import registry

    provider = registry.get_provider(slug)
    if provider and hasattr(provider, "token_hint"):
        return provider.token_hint
    hints = {
        "github": "ghp_...",
        "slack": "xoxb-... or xoxp-...",
        "notion": "secret_...",
        "stripe": "sk_...",
        "google": "OAuth token",
    }
    return hints.get(slug.replace("-mcp", ""), "API key")


def _provider_display_name(slug: str) -> str:
    cfg_name = get_provider(slug.replace("-mcp", ""))
    if cfg_name:
        return cfg_name.name
    hints = {"github-mcp": "GitHub", "slack-mcp": "Slack",
             "notion-mcp": "Notion", "stripe-mcp": "Stripe"}
    return hints.get(slug, slug)


def _is_provider_resource(resource: str) -> bool:
    if not resource:
        return False
    from app.providers import registry

    provider = registry.get_provider(resource)
    return bool(provider and getattr(provider, "requires_auth", False))


def _hidden_fields(client_id, redirect_uri, code_challenge, state, scope, resource) -> str:
    from html import escape

    vals = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "state": state,
        "scope": scope,
        "resource": resource,
    }
    return "".join(
        f'<input type="hidden" name="{escape(k, quote=True)}" value="{escape(str(v), quote=True)}">'
        for k, v in vals.items()
    )


def _client_display_name(db: Session, client_id: str) -> str:
    row = db.query(OAuthClient).filter(OAuthClient.client_id == client_id).first()
    return (row.client_name if row and row.client_name else client_id)


def _validate_client_params(db: Session, params: dict) -> None:
    """Re-validate client_id + redirect_uri against the registered client.

    The /authorize handler already did this via the SDK; a hostile human could
    tamper with the hidden fields on the consent form, so re-check the target
    redirect before minting a real authorization code.
    """
    row = db.query(OAuthClient).filter(OAuthClient.client_id == params["client_id"]).first()
    if not row:
        raise HTTPException(400, "unknown oauth client")
    allowed = json.loads(row.redirect_uris_json) if row.redirect_uris_json else []
    if not params["redirect_uri"]:
        raise HTTPException(400, "redirect_uri is required")
    if params["redirect_uri"] not in allowed:
        raise HTTPException(400, "redirect_uri not registered for this client")
    if not params["code_challenge"]:
        raise HTTPException(400, "code_challenge is required (PKCE)")


@router.get("/consent", response_class=HTMLResponse)
def consent_page(
    request: Request,
    db: Session = Depends(get_db),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    state: str = Query(""),
    scope: str = Query("mcp:tools"),
    resource: str = Query(""),
):
    user_id = read_session(request.cookies)
    client_name = _client_display_name(db, client_id)
    hidden = _hidden_fields(client_id, redirect_uri, code_challenge, state, scope, resource)
    if user_id is None:
        body = _FORM.format(hidden=hidden)
        return HTMLResponse(_PAGE.format(title="Sign in", body=body))
    if _is_provider_resource(resource):
        body = _provider_body(resource, client_name, hidden, state="")
        return HTMLResponse(_PAGE.format(title=f"Connect {_provider_display_name(resource)}", body=body))
    body = _CONSENT.format(client_name=client_name, scopes=scope, hidden=hidden)
    return HTMLResponse(_PAGE.format(title="Approve access", body=body))


def _provider_body(resource: str, client_name: str, hidden: str, state: str) -> str:
    provider_name = _provider_display_name(resource)
    cfg = get_provider(resource.replace("-mcp", ""))
    oauth_ready = bool(cfg and cfg.configured)
    if oauth_ready:
        connect_ui = _PROVIDER_APPROVE.format(hidden=hidden, provider_name=provider_name)
    else:
        hint = _provider_hint(resource)
        error = f'<p class="alt" style="color:#dc2626">{_escape(state)}</p>' if state else ""
        connect_ui = _PROVIDER_TOKEN.format(
            hidden=hidden, hint=hint, error=error, provider_name=provider_name,
        )
    return _PROVIDER_CONNECT.format(
        provider_name=provider_name, resource=resource, connect_ui=connect_ui,
    )


@router.post("/consent")
def consent_submit(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    email: str = Form(""),
    password: str = Form(""),
    signup: str = Form("0"),
    approve: str = Form("0"),
    token: str = Form(""),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    code_challenge: str = Form(""),
    state: str = Form(""),
    scope: str = Form("mcp:tools"),
    resource: str = Form(""),
):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "state": state,
        "scope": scope,
        "resource": resource,
    }

    existing_user_id = read_session(request.cookies)
    _validate_client_params(db, params)

    provider_resource = resource if _is_provider_resource(resource) else None

    if approve == "1":
        if not existing_user_id and not provider_resource:
            raise HTTPException(400, "approval requires a signed-in session")
        if provider_resource:
            return _consent_provider_connect(
                existing_user_id, provider_resource, params, token, db, response
            )
        code = issue_authorization_code(
            client_id=params["client_id"],
            user_id=existing_user_id,
            redirect_uri=params["redirect_uri"],
            code_challenge=params["code_challenge"],
            scope=params["scope"],
            db=db,
        )
        sep = "&" if "?" in params["redirect_uri"] else "?"
        return RedirectResponse(
            f'{params["redirect_uri"]}{sep}{urlencode({"code": code, "state": params["state"]})}',
            status_code=303,
        )

    if not email or not password:
        raise HTTPException(400, "email and password are required")

    user = get_oauth_user_by_email(db, email)
    if signup != "1":
        if not user or not user.check_password(password):
            raise HTTPException(401, "invalid email or password")
    else:
        if user:
            raise HTTPException(409, "email is already registered — sign in instead")
        if len(password) < 8:
            raise HTTPException(400, "password must be at least 8 characters")
        user = User(id=str(uuid.uuid4()), email=email.lower().strip())
        user.set_password(password)
        db.add(user)
        log_event(db, "user_created", channel="mcp_oauth", email=user.email)
        db.commit()

    set_session_cookie(response, user.id)
    client_name = _client_display_name(db, params["client_id"])
    hidden = _hidden_fields(
        params["client_id"], params["redirect_uri"], params["code_challenge"],
        params["state"], params["scope"], params["resource"],
    )
    if provider_resource:
        body = _provider_body(provider_resource, client_name, hidden, state="")
        page = HTMLResponse(
            _PAGE.format(title=f"Connect {_provider_display_name(provider_resource)}", body=body),
            status_code=200,
        )
        set_session_cookie(page, user.id)
        return page
    body = _CONSENT.format(client_name=client_name, scopes=params["scope"], hidden=hidden)
    page = HTMLResponse(_PAGE.format(title="Approve access", body=body), status_code=200)
    set_session_cookie(page, user.id)
    return page


def _consent_provider_connect(
    user_id: str | None,
    resource: str,
    params: dict,
    token: str,
    db: Session,
    response: Response,
):
    """Finish a provider-resource consent: store the credential (paste path) or
    hand off to the provider's OAuth (click-approve path), then continue the MCP
    authorization-code flow."""
    provider_name = _provider_display_name(resource)
    cfg = get_provider(resource.replace("-mcp", ""))
    oauth_ready = bool(cfg and cfg.configured)

    if not token.strip():
        if oauth_ready and user_id:
            response.set_cookie(
                "nxt_pending",
                wrap_pending_cookie(
                    user_id,
                    params["client_id"],
                    params["redirect_uri"],
                    params["code_challenge"],
                    params["state"],
                    params["scope"],
                    params["resource"],
                ),
                max_age=600,
                httponly=True,
                samesite="lax",
            )
            return RedirectResponse(
                f"/api/v1/oauth/{resource.replace('-mcp', '')}/authorize", status_code=303
            )
        if user_id:
            raise HTTPException(400, "a credential is required to connect this tool")
        raise HTTPException(400, "sign in first to connect this tool")

    if not user_id:
        raise HTTPException(400, "sign in first to connect this tool")
    _store_provider_credential(db, user_id, resource, token.strip())
    code = issue_authorization_code(
        client_id=params["client_id"],
        user_id=user_id,
        redirect_uri=params["redirect_uri"],
        code_challenge=params["code_challenge"],
        scope=params["scope"],
        db=db,
    )
    sep = "&" if "?" in params["redirect_uri"] else "?"
    return RedirectResponse(
        f'{params["redirect_uri"]}{sep}{urlencode({"code": code, "state": params["state"]})}',
        status_code=303,
    )


def _store_provider_credential(db: Session, user_id: str, provider_slug: str, token_value: str) -> None:
    db.query(Connection).filter(
        Connection.user_id == user_id, Connection.provider_slug == provider_slug
    ).delete()
    conn = Connection(
        id=str(uuid.uuid4()),
        user_id=user_id,
        provider_slug=provider_slug,
        credential_json=encrypt(json.dumps({"access_token": token_value})),
    )
    db.add(conn)
    log_event(db, "connection_created", channel="mcp_oauth", user_id=user_id, provider=provider_slug)
    db.commit()