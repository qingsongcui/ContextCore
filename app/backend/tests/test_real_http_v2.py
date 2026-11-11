"""Real HTTP process acceptance using only synthetic data and an isolated HOME."""
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx


def test_real_http_mcp_and_shared_context_without_compute(tmp_path, monkeypatch):
    data = tmp_path / "data"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CONTEXTCORE_DB", str(data / "contextcore.db"))
    from app.database import create_schema, seed_data, db
    create_schema()
    seed_data()
    credential = "synthetic-http-acceptance-only"
    with db() as conn:
        conn.execute("UPDATE connections SET enabled=1, token_hash=? WHERE id='codex'",
                     (hashlib.sha256(credential.encode()).hexdigest(),))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "HOME": str(home), "CONTEXTCORE_DATA_DIR": str(data)}
    with (tmp_path / "server.log").open("w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "app.cli", "serve", "--host", "127.0.0.1",
                                 "--port", str(port), "--data-dir", str(data)],
                                cwd=Path(__file__).parents[1], env=env, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.05)
                else:
                    raise AssertionError("Isolated HTTP service failed to start")
                csrf = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
                project = client.post("/api/projects", headers=csrf, json={"name": "ContextCore 合成验收 HTTP"}).json()
                memory = client.post("/api/content", headers=csrf, json={"project_id": project["id"], "title": "合成约束",
                    "category": "decision", "content": "合成约束：端口为18420；等待确认后继续。"}).json()
                headers = {"Authorization": f"Bearer {credential}", "Accept": "application/json, text/event-stream"}
                init = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 1,
                    "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "synthetic-http-acceptance", "version": "1"}}})
                assert init.status_code == 200 and "result" in init.json()
                tools = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                assert "assemble_context" in {t["name"] for t in tools.json()["result"]["tools"]}
                call = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "assemble_context", "arguments": {"task_query": "端口", "project_id": project["id"]}}})
                assert call.status_code == 200 and "18420" in call.text
                routing = client.post("/api/routing/v1/chat/completions", json={"model": "synthetic", "messages": []})
                assert routing.status_code in {401, 403, 503}
                assert client.get("/api/settings").status_code == 200
                assert client.delete(f"/api/content/{memory['id']}?expected_version={memory['version']}", headers=csrf).status_code == 200
                result = client.post("/api/retrieval/search", headers=csrf, json={"query": "端口"})
                assert result.status_code == 200 and result.json()["items"] == []
                assert not list(home.rglob("*.token"))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
