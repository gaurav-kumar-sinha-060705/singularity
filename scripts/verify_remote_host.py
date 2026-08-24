"""One-shot: prove /mcp accepts requests addressed to the deployed public hostname."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["COMPASS_DATABASE_URL"] = "sqlite:///./data/verify.db"
os.environ["RENDER_EXTERNAL_URL"] = "https://compass-gateway-fake.onrender.com"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "verify", "version": "0"},
    },
}

with TestClient(app) as client:
    # 1) Public Render hostname must pass the DNS-rebinding guard
    ok = client.post(
        "/mcp", json=INITIALIZE,
        headers={"Host": "compass-gateway-fake.onrender.com",
                 "Accept": "application/json, text/event-stream"},
    )
    assert ok.status_code == 200, f"public host rejected: {ok.status_code} {ok.text[:200]}"
    assert ok.json()["result"]["serverInfo"]["name"] == "compass"
    print("PASS  initialize via public hostname -> 200")

    # 2) Unknown host must still be rejected (guard stays ON)
    bad = client.post(
        "/mcp", json=INITIALIZE,
        headers={"Host": "evil-rebind.example.com",
                 "Accept": "application/json, text/event-stream"},
    )
    assert bad.status_code == 421, f"evil host not rejected: {bad.status_code}"
    print("PASS  hostile hostname still rejected -> 421")

print("REMOTE-HOST SIMULATION: ALL PASS")
