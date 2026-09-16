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

import base64
import hashlib
import hmac
import json
import time as _time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.mcp_oauth import get_oauth_user_by_email, issue_authorization_code
from app.models import OAuthClient, User, log_event

router = APIRouter(prefix="/mcp-auth", tags=["mcp-auth"])

MCP_SESSION_TTL_SECONDS = 1800

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


def _sign(payload: str) -> str:
    secret = get_settings().jwt_secret
    return base64.urlsafe_b64encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()


def _wrap_cookie(user_id: str) -> str:
    payload = {"u": user_id, "exp": int(_time.time()) + MCP_SESSION_TTL_SECONDS}
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    return f"{raw}.{_sign(raw)}"


def _read_cookie(cookies: dict) -> str | None:
    value = cookies.get("mcp_session")
    if not value:
        return None
    try:
        raw, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(raw)):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if payload.get("exp", 0) < _time.time():
            return None
        return payload.get("u")
    except Exception:
        return None


def _set_session_cookie(response: Response, user_id: str) -> None:
    response.set_cookie(
        "mcp_session",
        _wrap_cookie(user_id),
        max_age=MCP_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )


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
    user_id = _read_cookie(request.cookies)
    client_name = _client_display_name(db, client_id)
    hidden = _hidden_fields(client_id, redirect_uri, code_challenge, state, scope, resource)
    if user_id is None:
        body = _FORM.format(hidden=hidden)
        return HTMLResponse(_PAGE.format(title="Sign in", body=body))
    body = _CONSENT.format(client_name=client_name, scopes=scope, hidden=hidden)
    return HTMLResponse(_PAGE.format(title="Approve access", body=body))


@router.post("/consent")
def consent_submit(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    email: str = Form(""),
    password: str = Form(""),
    signup: str = Form("0"),
    approve: str = Form("0"),
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

    existing_user_id = _read_cookie(request.cookies)
    _validate_client_params(db, params)

    if approve == "1":
        if not existing_user_id:
            raise HTTPException(400, "approval requires a signed-in session")
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
        import uuid
        user = User(id=str(uuid.uuid4()), email=email.lower().strip())
        user.set_password(password)
        db.add(user)
        log_event(db, "user_created", channel="mcp_oauth", email=user.email)
        db.commit()

    _set_session_cookie(response, user.id)
    client_name = _client_display_name(db, params["client_id"])
    hidden = _hidden_fields(
        params["client_id"], params["redirect_uri"], params["code_challenge"],
        params["state"], params["scope"], params["resource"],
    )
    body = _CONSENT.format(client_name=client_name, scopes=params["scope"], hidden=hidden)
    page = HTMLResponse(_PAGE.format(title="Approve access", body=body), status_code=200)
    page.set_cookie(
        "mcp_session",
        _wrap_cookie(user.id),
        max_age=MCP_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return page