from app.database import SessionLocal
from app.models import AuditLog, Tool
from app.providers import registry


def _mock_weather_http(monkeypatch):
    def fake_get_json(url, params=None, timeout=None):
        if "geocoding-api" in url:
            return {"results": [
                {"name": "London", "country": "United Kingdom",
                 "latitude": 51.5074, "longitude": -0.1278, "timezone": "Europe/London"},
            ]}
        return {
            "current": {"temperature_2m": 18.2, "weather_code": 61,
                        "relative_humidity_2m": 76, "apparent_temperature": 17.5,
                        "wind_speed_10m": 14.0, "is_day": 1, "time": "2026-09-16T12:00"},
            "current_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h"},
        }
    monkeypatch.setattr("app.providers.weather.http_get_json", fake_get_json)


def _audit_events(event, decision=None):
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.event == event).all()
        out = []
        for row in rows:
            detail = getattr(row, "detail_json", None)
            import json
            parsed = json.loads(detail) if detail else {}
            if decision is None or parsed.get("decision") == decision:
                out.append((parsed.get("provider"), parsed))
        return out
    finally:
        db.close()


def test_execute_weather_ok(client, monkeypatch):
    _mock_weather_http(monkeypatch)
    resp = client.post("/api/v1/execute", json={
        "provider_slug": "weather",
        "arguments": {"location": "London"},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["provider"] == "weather"
    assert body["audited"] is True
    assert body["result"]["temperature"] == 18.2
    assert body["scope"] == "public:read"

    events = _audit_events("tool_executed", decision="allowed")
    assert any(provider == "weather" for provider, _ in events)


def test_execute_unknown_provider(client):
    resp = client.post("/api/v1/execute", json={
        "provider_slug": "github-mcp",
        "arguments": {},
    })
    assert resp.status_code == 404
    assert "no gateway provider" in resp.json()["detail"]


def test_execute_invalid_scope(client, monkeypatch):
    _mock_weather_http(monkeypatch)
    resp = client.post("/api/v1/execute", json={
        "provider_slug": "weather",
        "arguments": {"location": "London"},
        "scope": "github:write",
    })
    assert resp.status_code == 403
    assert "not granted" in resp.json()["detail"]

    events = _audit_events("tool_executed", decision="denied")
    assert any(provider == "weather" for provider, _ in events)


def test_execute_blocks_low_trust(client, monkeypatch):
    _mock_weather_http(monkeypatch)
    db = SessionLocal()
    try:
        tool = db.query(Tool).filter(Tool.slug == "weather").first()
        old = tool.trust_score
        tool.trust_score = 0.1
        db.commit()
    finally:
        db.close()
    try:
        resp = client.post("/api/v1/execute", json={
            "provider_slug": "weather",
            "arguments": {"location": "London"},
        })
        assert resp.status_code == 403
        assert "blocked" in resp.json()["detail"]

        events = _audit_events("tool_executed", decision="denied")
        assert any(provider == "weather" for provider, _ in events)
    finally:
        db = SessionLocal()
        try:
            tool = db.query(Tool).filter(Tool.slug == "weather").first()
            tool.trust_score = old
            db.commit()
        finally:
            db.close()


def test_execute_failed_upstream(client, monkeypatch):
    def boom(url, params=None, timeout=None):
        raise RuntimeError("connection refused")
    monkeypatch.setattr("app.providers.weather.http_get_json", boom)
    resp = client.post("/api/v1/execute", json={
        "provider_slug": "weather",
        "arguments": {"location": "London"},
    })
    assert resp.status_code == 502
    assert "connection refused" in resp.json()["detail"].lower()


def test_execute_rejects_empty_slug(client):
    resp = client.post("/api/v1/execute", json={"provider_slug": "", "arguments": {}})
    assert resp.status_code == 422


def test_providers_are_indexed_as_tools(client):
    resp = client.get("/api/v1/tools")
    slugs = {t["slug"] for t in resp.json()["tools"]}
    assert {"weather", "npms_lookup", "pypi_lookup", "web_search"} <= slugs


def test_provider_tools_are_clean_first_party(client):
    resp = client.get("/api/v1/tools")
    by_slug = {t["slug"]: t for t in resp.json()["tools"]}
    for slug in ("weather", "npms_lookup", "pypi_lookup", "web_search"):
        tool = by_slug[slug]
        assert tool["publisher"] == "Singularity"
        assert tool["publisher_verified"] is True
        assert tool["trust_flags"] == []
        assert tool["permissions_requested"] == ["public:read"]
    assert registry.get_provider("weather") is not None