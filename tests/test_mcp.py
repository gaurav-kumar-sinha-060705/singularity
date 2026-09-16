import json

from app.mcp_server import call_tool, compare_tools, find_solutions, get_trust_report, list_public_tools


def test_find_solutions_ranks_finance_first():
    out = find_solutions("I need to track my team's expenses and submit receipts")
    assert "expensify-mcp" in out
    assert "trust" in out.lower()
    assert "Singularity advises" in out
    assert "quickledger-pro" not in out


def test_find_solutions_excludes_dangerously_low_trust():
    out = find_solutions("track expenses", top_k=10)
    assert "quickledger-pro" not in out


def test_find_solutions_no_match_returns_honest_fallback():
    out = find_solutions("auto-generate video captions for my YouTube channel")
    assert "No strong matches" in out
    assert "fit 1.0" not in out
    assert "Phase 1" in out


def test_find_solutions_wedding_returns_no_match():
    out = find_solutions("help me plan a wedding seating chart")
    assert "No strong matches" in out
    assert "fit 1.0" not in out


def test_get_trust_report_shows_overreach():
    out = get_trust_report("github-mcp")
    assert "permission_overreach" in out
    assert "repo:write" in out
    assert "verified: yes" in out


def test_get_trust_report_unknown_slug():
    out = get_trust_report("no-such-tool")
    assert "No tool indexed" in out
    assert "find_solutions" in out


def test_compare_tools_table():
    out = compare_tools(["github-mcp", "slack-mcp"])
    assert "| `github-mcp` |" in out
    assert "| `slack-mcp` |" in out
    assert "| slug |" in out


def test_compare_tools_needs_two_known():
    out = compare_tools(["github-mcp", "bogus-slug"])
    assert "at least two" in out


def _rpc(method: str, params: dict, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}


def _parse_response(resp):
    body = resp.text
    if body.startswith("data:") or "\ndata:" in body:
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"unparsable SSE: {body[:200]}")
    return resp.json()


INITIALIZE = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "0.0.1"},
}
HEADERS = {"Accept": "application/json, text/event-stream"}


def test_mcp_endpoint_initialize(client):
    resp = client.post("/mcp", json=_rpc("initialize", INITIALIZE), headers=HEADERS)
    assert resp.status_code == 200, resp.text
    body = _parse_response(resp)
    info = body["result"]["serverInfo"]
    assert info["name"] == "singularity"


def test_mcp_endpoint_list_and_call_tool(client):
    client.post("/mcp", json=_rpc("initialize", INITIALIZE), headers=HEADERS)
    resp = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "find_solutions",
                                 "arguments": {"problem": "monitor errors in production"}},
                  id_=2),
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = _parse_response(resp)
    content = body["result"]["content"]
    text = "".join(c.get("text", "") for c in content)
    assert "Top solutions" in text


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


def test_list_public_tools_lists_providers():
    out = list_public_tools()
    assert "weather" in out
    assert "npms_lookup" in out
    assert "call_tool" in out


def test_call_tool_weather_executes(monkeypatch):
    _mock_weather_http(monkeypatch)
    out = call_tool("weather", {"location": "London"})
    assert "executed through the audited gateway" in out
    assert "temperature" in out


def test_call_tool_unknown_provider():
    out = call_tool("totally-unregistered-mcp", {})
    assert "Could not execute" in out
    assert "no gateway provider" in out


def test_call_tool_denied_scope(monkeypatch):
    _mock_weather_http(monkeypatch)
    out = call_tool("weather", {"location": "London"}, scope="github:write")
    assert "denied" in out
    assert "not granted" in out
