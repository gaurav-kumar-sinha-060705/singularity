"""Singularity dashboard: a small web page to sign in and connect MCP tool
credentials (OAuth or API key), then verify them against the hosted remote
before calling from a client like Claude.

Servers a single self-contained static page (no build step); all data flows
through the existing REST API with the account's access token.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(tags=["dashboard"])

_DASHBOARD = (Path(__file__).resolve().parent.parent / "static" / "dashboard.html")
_AUTHORIZE = (Path(__file__).resolve().parent.parent / "static" / "authorize.html")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    return HTMLResponse(
        _DASHBOARD.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/authorize/{provider_slug}", response_class=HTMLResponse)
def authorize_page(provider_slug: str) -> HTMLResponse:
    """Pop-up-style page: sign in + click Approve to connect a tool's credential.

    The approve action is a plain link to the singed-out of GET /api/v1/oauth/
    {slug}/authorize when the provider's OAuth app is configured; otherwise it
    falls back to pasting a token (stored in the vault) inside the popup.
    """
    return HTMLResponse(
        _AUTHORIZE.read_text(encoding="utf-8").replace("__PROVIDER_SLUG__", provider_slug),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/connections", response_class=RedirectResponse, include_in_schema=False)
def connections() -> RedirectResponse:
    return RedirectResponse("/dashboard", status_code=302)


@router.get("/", response_class=RedirectResponse, include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/dashboard", status_code=302)