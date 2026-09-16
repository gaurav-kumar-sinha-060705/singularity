from fastapi import Request

from app.models import hash_ip


def extract_audit_context(request: Request) -> dict:
    channel = request.headers.get("x-test-source", "rest")
    raw_ip = (
        request.headers.get("x-forwarded-for", "")
        or (request.client.host if request.client else "unknown")
    )
    ip = raw_ip.split(",")[0].strip() or "unknown"
    return {"channel": channel, "ip_hash": hash_ip(ip)}