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


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    return HTMLResponse(
        _DASHBOARD.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/", response_class=RedirectResponse, include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/dashboard", status_code=302)