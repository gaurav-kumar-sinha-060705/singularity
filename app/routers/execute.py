from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.audit import extract_audit_context
from app.auth import get_optional_user
from app.database import get_db
from app.models import User
from app.schemas import ExecuteRequest, ExecuteResponse
from app.services.gateway import execute_via_gateway

router = APIRouter()


@router.post("/execute", response_model=ExecuteResponse)
def execute(payload: ExecuteRequest, request: Request, db: Session = Depends(get_db),
            user: User | None = Depends(get_optional_user)):
    audit = extract_audit_context(request)
    out = execute_via_gateway(
        payload.provider_slug,
        payload.arguments,
        scope=payload.scope,
        channel=audit["channel"],
        db=db,
        ip_hash=audit["ip_hash"],
        user_id=user.id if user else None,
    )

    if out["decision"] == "not_found":
        raise HTTPException(status_code=404, detail=out["reason"])
    if out["decision"] == "disabled":
        raise HTTPException(status_code=503, detail=out["reason"])
    if out["decision"] == "denied":
        raise HTTPException(status_code=403, detail=out["reason"])
    if out["decision"] == "failed":
        raise HTTPException(status_code=502, detail=out["reason"])

    return ExecuteResponse(
        provider=out["provider"],
        version=out["version"],
        scope=out["scope"],
        audited=True,
        ok=True,
        result=out.get("result"),
        latency_ms=out.get("latency_ms", 0),
    )