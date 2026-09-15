from singularity.services.scanner import scan_permissions, scan_tool


def test_poisoned_sample_is_flagged():
    import json
    from pathlib import Path

    entries = {e["slug"]: e for e in json.loads(
        (Path(__file__).resolve().parents[1] / "data" / "seed_tools.json").read_text(encoding="utf-8")
    )["tools"]}
    flags = scan_tool(entries["quickledger-pro"])
    assert "suspicious_description_imperative" in flags
    assert "hidden_unicode_characters" in flags
    assert "permission_overreach" in flags
    assert "unverified_publisher" in flags


def test_clean_tool_has_no_injection_flags():
    flags = scan_tool({
        "description": "Read channel history and post messages in Slack workspaces.",
        "permissions_requested": ["channels:read", "chat:write"],
        "permissions_needed": ["channels:read", "chat:write"],
        "publisher_verified": True,
    })
    assert flags == []


def test_overreach_detection():
    flags = scan_permissions(["expenses:read", "credentials:read"], ["expenses:read"])
    assert "permission_overreach" in flags
    assert "sensitive_permission_overreach" in flags
