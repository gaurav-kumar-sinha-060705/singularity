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


def test_recommend_expenses_ranks_finance_first(client):
    resp = client.post("/api/v1/recommend", json={
        "problem": "I need to track my team's expenses and submit receipts",
        "top_k": 5,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"]["category_hint"] == "finance"
    recs = body["recommendations"]
    assert len(recs) >= 1
    assert recs[0]["category"] == "finance"
    slugs = [r["slug"] for r in recs]
    assert "quickledger-pro" not in slugs
    assert "slack-mcp" not in slugs
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


def test_recommend_no_match_returns_empty_with_message(client):
    resp = client.post("/api/v1/recommend", json={
        "problem": "auto-generate video captions for my YouTube channel",
        "top_k": 5,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["recommendations"] == []
    assert body["message"] is not None
    assert "No strong match" in body["message"]
    assert body["intent"]["category_hint"] is None


def test_recommend_expenses_filters_low_fit_tools(client):
    resp = client.post("/api/v1/recommend", json={
        "problem": "I need to track my team's expenses and submit receipts",
        "top_k": 10,
    })
    assert resp.status_code == 200
    slugs = [r["slug"] for r in resp.json()["recommendations"]]
    assert "slack-mcp" not in slugs
    assert "sentry-mcp" not in slugs


def test_off_topic_wedding_returns_no_match(client):
    resp = client.post("/api/v1/recommend", json={
        "problem": "help me plan a wedding seating chart",
        "top_k": 5,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["recommendations"] == []
    assert body["message"] is not None
    assert body["intent"]["category_hint"] is None


def test_off_topic_freelance_contracts_returns_no_match(client):
    resp = client.post("/api/v1/recommend", json={
        "problem": "draft freelance contracts for my design clients",
        "top_k": 5,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["recommendations"] == []
    assert body["message"] is not None
    assert body["intent"]["category_hint"] is None
