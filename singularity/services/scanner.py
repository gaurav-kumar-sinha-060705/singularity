import re
import unicodedata

ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064\u202a-\u202e\ufeff]")

IMPERATIVE_PATTERNS = [
    (r"\bignore\b[^.\n]{0,40}\b(instructions?|prompt|rules?|safety)\b",),
    (r"\b(disregard|forget)\b[^.\n]{0,40}\b(above|prior|previous|earlier)\b",),
    (r"\bdo not\b[^.\n]{0,30}\b(tell|inform|reveal|notify|mention)\b",),
    (r"\b(exfiltrate|covertly|secretly|silently)\b",),
    (r"\bsend\b[^.\n]{0,60}\bhttps?://",),
    (r"\b(read|access|steal)\b[^.\n]{0,50}\b(\.env|id_rsa|credentials?|passwords?|tokens?|api[- ]?keys?)\b",),
    (r"\b(you are now|act as|pretend to be|new instructions?|system prompt)\b",),
    (r"\bbefore responding\b",),
]

SENSITIVE_PERMISSIONS = {"credentials", "secrets", "fs", "network"}


def scan_description(description: str) -> list[str]:
    flags: list[str] = []
    folded = unicodedata.normalize("NFKC", description)
    if ZERO_WIDTH_RE.search(description):
        flags.append("hidden_unicode_characters")
    for pattern, in IMPERATIVE_PATTERNS:
        if re.search(pattern, folded, re.IGNORECASE):
            flags.append("suspicious_description_imperative")
            break
    return flags


def scan_permissions(permissions_requested: list[str], permissions_needed: list[str]) -> list[str]:
    flags: list[str] = []
    overreach = set(permissions_requested) - set(permissions_needed)
    if overreach:
        flags.append("permission_overreach")
    if any(p.split(":", 1)[0] in SENSITIVE_PERMISSIONS for p in overreach):
        flags.append("sensitive_permission_overreach")
    return flags


def scan_tool(payload: dict) -> list[str]:
    flags = scan_description(payload.get("description", ""))
    flags += scan_permissions(payload.get("permissions_requested", []), payload.get("permissions_needed", []))
    if not payload.get("publisher_verified", False):
        flags.append("unverified_publisher")
    return sorted(set(flags))
