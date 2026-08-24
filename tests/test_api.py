def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["indexed_tools"] >= 15


def test_tools_list(client):
    resp = client.get("/api/v1/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 15
    slugs = {t["slug"] for t in body["tools"]}
    assert "quickledger-pro" in slugs


def test_tools_detail(client):
    resp = client.get("/api/v1/tools/github-mcp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["publisher_verified"] is True
    assert "repo:write" in body["permissions_requested"]
    assert "permission_overreach" in body["trust_flags"]


def test_recommend_expenses_ranks_finance_first_and_buries_poisoned_tool(client):
    resp = client.post("/api/v1/recommend", json={
        "problem": "I need to track my team's expenses and submit receipts",
        "top_k": 5,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"]["category_hint"] == "finance"
    recs = body["recommendations"]
    assert len(recs) == 5
    top3_categories = [r["category"] for r in recs[:3]]
    assert top3_categories.count("finance") >= 2
    poisoned = next((r for r in recs if r["slug"] == "quickledger-pro"), None)
    if poisoned:
        assert recs.index(poisoned) >= 3
        assert "suspicious_description_imperative" in poisoned["trust_flags"]
    for r in recs:
        assert 0.0 <= r["fit_score"] <= 1.0
        assert 0.0 <= r["rank_score"] <= 1.0
        assert r["rationale"]


def test_recommend_respects_filters(client):
    resp = client.post("/api/v1/recommend", json={
        "problem": "monitor errors in my production app",
        "top_k": 10,
        "filters": {"max_pricing_tier": "free"},
    })
    assert resp.status_code == 200
    for r in resp.json()["recommendations"]:
        assert r["pricing_tier"] == "free"


def test_recommend_validates_input(client):
    resp = client.post("/api/v1/recommend", json={"problem": ""})
    assert resp.status_code == 422


def test_unknown_tool_404(client):
    assert client.get("/api/v1/tools/does-not-exist").status_code == 404
