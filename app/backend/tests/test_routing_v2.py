import hashlib
import json
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app.database import create_schema, db, get_app_setting
from app.services import routing_service as svc, usage_service
from app.routers.routing import router

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "routing.db"))
    create_schema(tmp_path / "routing.db")
    with db() as conn:
        for pid in ("p", "backup", "a", "b"):
            conn.execute(
                "INSERT INTO management_entities VALUES(?,?,?,1,?,?)",
                (pid, "provider", json.dumps({"name": pid, "protocol": "openai", "base_url": f"https://{pid}.example/v1", "enabled": True}), "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    token = svc.rotate_token(True)["token"]
    monkeypatch.setattr(svc, "resolve_provider", lambda _: {"id": "p", "base_url": "https://upstream.example/v1", "protocol": "openai", "api_key": "private-upstream-secret", "model_map": {"alias": "actual"}, "enabled": True})
    svc.configure({"enabled": True, "provider_id": "p"})
    return client, token, monkeypatch

@pytest.fixture
def anyio_backend():
    return "asyncio"

def transport(monkeypatch, handler):
    monkeypatch.setattr(svc, "client_factory", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout))

class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, fail=False):
        self.chunks, self.fail, self.closed, self.reads = chunks, fail, False, 0
    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.fail:
            raise httpx.ReadError("secret in upstream exception")
    async def aclose(self):
        self.closed = True

def test_default_disabled_token_auth_and_rotation(env):
    client, token, monkeypatch = env
    assert "token" not in client.get("/api/routing/settings").json()
    assert "token" not in client.get("/api/routing/status").json()
    assert get_app_setting("routing_v2_token_hash") == hashlib.sha256(token.encode()).hexdigest()
    assert client.post("/api/routing/v1/chat/completions", json={"model": "m"}).status_code == 401
    assert client.post("/api/routing/v1/chat/completions", json={"model": "m"}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/routing/token", json={"confirmed": False}).status_code == 422
    svc.configure({"enabled": False})
    assert client.post("/api/routing/v1/chat/completions", json={"model": "m"}, headers={"Authorization": "Bearer " + token}).status_code == 409
    svc.rotate_token(True)
    with pytest.raises(HTTPException):
        svc.authenticate("Bearer " + token)

def test_nonstream_passthrough_request_model_usage_and_no_secret_ledger(env):
    client, token, monkeypatch = env
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url == "https://upstream.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer private-upstream-secret"
        assert json.loads(request.content)["model"] == "actual"
        return httpx.Response(200, json={"id": "upstream-1", "model": "actual", "choices": [{"message": {"content": "private body"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    transport(monkeypatch, handler)
    result = client.post("/api/routing/v1/chat/completions", json={"model": "alias", "messages": [{"role": "user", "content": "private prompt"}]}, headers={"Authorization": "Bearer " + token})
    assert result.status_code == 200
    assert result.json()["choices"][0]["message"]["content"] == "private body"
    assert len(calls) == 1
    row = usage_service.requests()["items"][0]
    assert row["status"] == "succeeded" and row["usage"]["output_tokens"] == 5
    assert row["performance"]["ttft_ms"] is None
    with db() as conn:
        stored = str([tuple(r) for r in conn.execute("SELECT * FROM usage_v2_observations")])
    for secret in ("private-upstream-secret", token, "private prompt", "private body"):
        assert secret not in stored

def test_sse_usage_only_heartbeat_does_not_count_as_first_content(env):
    client, token, monkeypatch = env
    stream = Chunks([b": heartbeat\n\n", b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":0}}\n\n', b'data: {"id":"s1","choices":[{"delta":{"content":"a"}}]}\n\n', b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n', b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\n', b"data: [DONE]\n\n"])
    def handler(request):
        assert json.loads(request.content)["stream_options"]["include_usage"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    transport(monkeypatch, handler)
    result = client.post("/api/routing/v1/chat/completions", json={"model": "m", "stream": True}, headers={"Authorization": "Bearer " + token})
    assert result.status_code == 200 and "[DONE]" in result.text
    row = usage_service.requests()["items"][0]
    assert row["content_chunks"] == 2 and row["status"] == "succeeded"
    assert row["usage"]["output_tokens"] == 2
    assert stream.closed and svc.status()["active_requests"] == 0

def test_partial_stream_unknown_no_retry(env):
    client, token, monkeypatch = env
    calls = []
    stream = Chunks([b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"write"}}]}}]}\n\n'], fail=True)
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    transport(monkeypatch, handler)
    result = client.post("/api/routing/v1/chat/completions", json={"model": "m", "stream": True}, headers={"Authorization": "Bearer " + token})
    assert len(calls) == 1 and "[DONE]" not in result.text
    assert "secret in upstream exception" not in result.text
    row = usage_service.requests()["items"][0]
    assert row["status"] == "unknown" and row["content_chunks"] == 1
    assert row["usage"]["output_tokens"] is None

def test_protocol_mismatch_background_redirect_and_upstream_failure(env):
    client, token, monkeypatch = env
    auth = {"Authorization": "Bearer " + token}
    assert client.post("/api/routing/v1/messages", json={"model": "m"}, headers=auth).status_code == 422
    assert client.post("/api/routing/v1/responses", json={"model": "m", "background": True}, headers=auth).status_code == 422
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://evil.example"}, text="private-upstream-secret")
    transport(monkeypatch, handler)
    result = client.post("/api/routing/v1/chat/completions", json={"model": "m"}, headers=auth, follow_redirects=False)
    assert len(calls) == 1 and result.status_code == 307
    assert "private-upstream-secret" not in result.text and "location" not in result.headers
    assert usage_service.requests()["items"][0]["status"] == "failed"

def test_anthropic_usage_split_and_tools_passthrough(env):
    client, token, monkeypatch = env
    monkeypatch.setattr(svc, "resolve_provider", lambda _: {"base_url": "https://anthropic.example/v1", "protocol": "anthropic", "api_key": "anthropic-secret"})
    chunks = [b'data: {"type":"message_start","message":{"id":"a1","model":"claude","usage":{"input_tokens":10,"cache_read_input_tokens":5,"output_tokens":0}}}\n\n', b'data: {"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"{}"}}\n\n', b'data: {"type":"message_delta","usage":{"output_tokens":4}}\n\n', b'data: {"type":"message_stop"}\n\n']
    def handler(request):
        assert request.headers["x-api-key"] == "anthropic-secret"
        assert "authorization" not in request.headers
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(chunks))
    transport(monkeypatch, handler)
    result = client.post("/api/routing/v1/messages", json={"model": "claude", "max_tokens": 10, "stream": True}, headers={"Authorization": "Bearer " + token})
    assert result.status_code == 200
    row = usage_service.requests()["items"][0]
    assert row["usage"]["input_tokens"] == 15 and row["usage"]["output_tokens"] == 4
    assert row["status"] == "succeeded" and row["content_chunks"] == 1

@pytest.mark.anyio
async def test_backpressure_and_cancel_before_next_upstream_chunk(env):
    client, token, monkeypatch = env
    stream = Chunks([b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n', b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n', b"data: [DONE]\n\n"])
    transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream))
    class Request:
        headers = {"authorization": "Bearer " + token}
        disconnected = False
        async def stream(self):
            yield b'{"model":"m","stream":true}'
        async def is_disconnected(self):
            return self.disconnected
    request = Request()
    response = await svc.forward(request, "chat/completions")
    assert stream.reads == 0
    iterator = response.body_iterator
    await anext(iterator)
    assert stream.reads == 1
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert stream.closed and usage_service.requests()["items"][0]["status"] == "cancelled"

def test_startup_recovery_marks_unknown_never_resends(env):
    client, token, monkeypatch = env
    usage_service.ingest([{"origin": "proxy", "observation_id": "started", "attempt_id": "dead", "started_at": "2026-01-01T00:00:00Z", "status": "running"}])
    assert svc.recover_interrupted_attempts() == 1
    assert usage_service.requests()["items"][0]["status"] == "unknown"
    assert svc.recover_interrupted_attempts() == 0


def test_explicit_safe_failover_has_one_logical_request_and_two_attempts(env):
    client, token, monkeypatch = env
    svc.configure({"fallback_provider_ids": ["backup"], "circuit_threshold": 1})
    monkeypatch.setattr(svc, "resolve_provider", lambda pid: {"base_url": f"https://{pid}.example/v1", "protocol": "openai"})
    calls = []
    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "p.example":
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(200, json={"id": "b", "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    transport(monkeypatch, handler)
    response = client.post("/api/routing/v1/chat/completions", json={"model": "m"}, headers={"authorization": "Bearer " + token})
    assert response.status_code == 200 and calls == ["p.example", "backup.example"]
    data = usage_service.summary()
    assert data["logical_requests"] == 1 and data["attempts"] == 2
    assert svc.status()["circuits"]["p"]["state"] == "open"
    assert data["status_counts"]["failed"] == 1
    client.post("/api/routing/v1/chat/completions", json={"model": "m"}, headers={"authorization": "Bearer " + token})
    assert calls[-1] == "backup.example" and calls.count("p.example") == 1


@pytest.mark.parametrize("exception", [httpx.ReadTimeout("unknown"), httpx.WriteError("unknown")])
def test_unknown_outcome_never_falls_back(env, exception):
    client, token, monkeypatch = env
    svc.configure({"fallback_provider_ids": ["backup"]})
    calls = []
    def handler(request):
        calls.append(request)
        raise exception
    transport(monkeypatch, handler)
    response = client.post("/api/routing/v1/chat/completions", json={"model": "m"}, headers={"authorization": "Bearer " + token})
    assert response.status_code == 502 and len(calls) == 1
    assert usage_service.requests()["items"][0]["status"] == "unknown"


def test_model_routes_round_robin_and_same_protocol_required(env):
    client, token, monkeypatch = env
    svc.configure({"model_routes": {"special": ["a", "b"]}, "routing_strategy": "round_robin"})
    monkeypatch.setattr(svc, "resolve_provider", lambda pid: {"base_url": f"https://{pid}.example/v1", "protocol": "openai"})
    calls = []
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    transport(monkeypatch, handler)
    for _ in range(2):
        assert client.post("/api/routing/v1/chat/completions", json={"model": "special"}, headers={"authorization": "Bearer " + token}).status_code == 200
    assert calls == ["a.example", "b.example"]
    monkeypatch.setattr(svc, "resolve_provider", lambda pid: {"base_url": f"https://{pid}.example/v1", "protocol": "openai" if pid == "a" else "anthropic"})
    assert client.post("/api/routing/v1/chat/completions", json={"model": "special"}, headers={"authorization": "Bearer " + token}).status_code == 422
    assert len(calls) == 2


def test_connection_failure_before_transmission_can_fall_back(env):
    client, token, monkeypatch = env
    svc.configure({"fallback_provider_ids": ["backup"]})
    monkeypatch.setattr(svc, "resolve_provider", lambda pid: {"base_url": f"https://{pid}.example/v1", "protocol": "openai"})
    calls = []
    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "p.example":
            raise httpx.ConnectError("connect failed")
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    transport(monkeypatch, handler)
    assert client.post("/api/routing/v1/chat/completions", json={"model": "m"}, headers={"authorization": "Bearer " + token}).status_code == 200
    assert calls == ["p.example", "backup.example"]
    assert usage_service.summary()["attempts"] == 2


def test_chat_to_anthropic_nonstream_tools_and_system_golden(env):
    client, token, monkeypatch = env
    monkeypatch.setattr(svc, "resolve_provider", lambda _: {"base_url": "https://claude.example/v1", "protocol": "anthropic"})
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages"
        assert body["system"] == "Keep exact IDs"
        assert body["tools"][0]["input_schema"] == {"type": "object"}
        assert body["messages"][1]["content"][0] == {"type": "tool_use", "id": "call-original", "name": "read", "input": {"path": "a"}}
        assert body["messages"][2]["content"][0]["tool_use_id"] == "call-original"
        return httpx.Response(200, json={"id": "anthro-id", "model": "claude", "content": [{"type": "text", "text": "result"}, {"type": "tool_use", "id": "next-id", "name": "write", "input": {"x": 1}}], "stop_reason": "tool_use", "usage": {"input_tokens": 10, "cache_read_input_tokens": 2, "output_tokens": 3}})
    transport(monkeypatch, handler)
    body = {"model": "claude", "max_tokens": 100, "messages": [{"role": "system", "content": "Keep exact IDs"}, {"role": "user", "content": "read"}, {"role": "assistant", "content": None, "tool_calls": [{"id": "call-original", "type": "function", "function": {"name": "read", "arguments": '{"path":"a"}'}}]}, {"role": "tool", "tool_call_id": "call-original", "content": "file"}], "tools": [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]}
    result = client.post("/api/routing/v1/chat/completions", json=body, headers={"authorization": "Bearer " + token})
    assert result.status_code == 200
    data = result.json()
    assert data["choices"][0]["message"]["tool_calls"][0]["id"] == "next-id"
    assert json.loads(data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {"x": 1}
    assert data["usage"]["prompt_tokens"] == 12


def test_anthropic_to_chat_nonstream_tools_and_usage_golden(env):
    client, token, monkeypatch = env
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert body["messages"][0] == {"role": "system", "content": "rules"}
        assert body["messages"][2]["tool_calls"][0]["id"] == "use-id"
        assert body["messages"][3]["tool_call_id"] == "use-id"
        return httpx.Response(200, json={"id": "chat", "model": "m", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "next", "type": "function", "function": {"name": "next", "arguments": '{"a":1}'}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 12, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 2}}})
    transport(monkeypatch, handler)
    body = {"model": "m", "max_tokens": 100, "system": "rules", "messages": [{"role": "user", "content": "read"}, {"role": "assistant", "content": [{"type": "tool_use", "id": "use-id", "name": "read", "input": {}}]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "use-id", "content": "result"}]}]}
    result = client.post("/api/routing/v1/messages", json=body, headers={"authorization": "Bearer " + token})
    assert result.status_code == 200
    data = result.json()
    assert data["content"][0] == {"type": "tool_use", "id": "next", "name": "next", "input": {"a": 1}}
    assert data["usage"] == {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 2}


def test_anthropic_stream_to_chat_preserves_tool_id_and_deltas(env):
    client, token, monkeypatch = env
    monkeypatch.setattr(svc, "resolve_provider", lambda _: {"base_url": "https://claude.example/v1", "protocol": "anthropic"})
    events = [{"type": "message_start", "message": {"id": "msg", "model": "c", "usage": {"input_tokens": 10, "output_tokens": 0}}}, {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "exact-id", "name": "read", "input": {}}}, {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"a":'}}, {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '1}'}}, {"type": "content_block_stop", "index": 0}, {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}, {"type": "message_stop"}]
    transport(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks([b"data: " + json.dumps(event).encode() + b"\n\n" for event in events])))
    result = client.post("/api/routing/v1/chat/completions", json={"model": "c", "max_tokens": 100, "stream": True, "messages": [{"role": "user", "content": "go"}]}, headers={"authorization": "Bearer " + token})
    assert result.status_code == 200 and result.text.endswith("data: [DONE]\n\n")
    parsed = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")]
    tool = [event["choices"][0]["delta"]["tool_calls"][0] for event in parsed if event["choices"] and event["choices"][0]["delta"].get("tool_calls")]
    assert tool[0]["id"] == "exact-id" and tool[0]["function"]["name"] == "read"
    assert "".join(t["function"].get("arguments", "") for t in tool) == '{"a":1}'
    assert usage_service.requests()["items"][0]["status"] == "succeeded"


def test_chat_stream_to_anthropic_buffered_usage_and_tool_deltas(env):
    client, token, monkeypatch = env
    events = [{"id": "chat-id", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant"}}]}, {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "exact-tool", "type": "function", "function": {"name": "read", "arguments": '{"x":'}}]}}]}, {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]}, "finish_reason": "tool_calls"}]}, {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 2}}}]
    chunks = [b"data: " + json.dumps(e).encode() + b"\n\n" for e in events] + [b"data: [DONE]\n\n"]
    transport(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(chunks)))
    result = client.post("/api/routing/v1/messages", json={"model": "m", "max_tokens": 100, "stream": True, "messages": [{"role": "user", "content": "go"}]}, headers={"authorization": "Bearer " + token})
    assert result.status_code == 200 and 'event: message_stop' in result.text
    parsed = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")]
    assert parsed[0]["message"]["usage"]["input_tokens"] == 8
    block = next(e for e in parsed if e["type"] == "content_block_start")
    assert block["content_block"]["id"] == "exact-tool"
    assert "".join(e["delta"]["partial_json"] for e in parsed if e["type"] == "content_block_delta") == '{"x":1}'
    assert usage_service.requests()["items"][0]["status"] == "succeeded"


def test_conversion_rejects_complex_fields_and_missing_terminal_is_unknown(env):
    client, token, monkeypatch = env
    auth = {"authorization": "Bearer " + token}
    request = {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]}
    assert client.post("/api/routing/v1/messages", json={**request, "thinking": {"type": "enabled"}}, headers=auth).status_code == 422
    assert client.post("/api/routing/v1/messages", json={**request, "messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]}, headers=auth).status_code == 422
    chunks = [b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n']
    transport(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(chunks)))
    response = client.post("/api/routing/v1/messages", json={**request, "stream": True}, headers=auth)
    assert 'message_stop' not in response.text
    assert usage_service.requests()["items"][0]["status"] == "unknown"


def test_done_without_final_usage_is_unknown(env):
    client, token, monkeypatch = env
    chunks = [b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\n', b'data: [DONE]\n\n']
    transport(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(chunks)))
    client.post("/api/routing/v1/chat/completions", json={"model": "m", "stream": True}, headers={"authorization": "Bearer " + token})
    row = usage_service.requests()["items"][0]
    assert row["status"] == "unknown" and row["usage"]["output_tokens"] is None


@pytest.mark.parametrize("field", ["id", "model"])
def test_known_opaque_provider_credential_echo_never_reaches_response_or_ledger(env, field):
    client, token, monkeypatch = env
    secret = "opaqueCredentialExactlyWithoutPrefix7654321"
    monkeypatch.setattr(svc, "resolve_provider", lambda _: {"base_url": "https://upstream.example/v1", "protocol": "openai", "api_key": secret})
    payload = {"id": "safe", "model": "safe", "choices": [{"message": {"content": "O"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}, field: "prefix-" + secret + "-suffix"}
    transport(monkeypatch, lambda req: httpx.Response(200, json=payload))
    response = client.post("/api/routing/v1/chat/completions", json={"model": "safe"}, headers={"authorization": "Bearer " + token})
    assert response.status_code == 502 and secret not in response.text
    assert usage_service.requests()["items"][0]["status"] == "unknown"
    with db() as conn:
        for table in ("usage_v2_attempts", "usage_v2_observations", "audit_logs"):
            assert secret not in str([tuple(r) for r in conn.execute("SELECT * FROM " + table)])


def test_stream_nested_provider_credential_echo_is_not_forwarded_or_recorded(env):
    client, token, monkeypatch = env
    secret = "opaqueCredentialExactlyWithoutPrefix7654321"
    monkeypatch.setattr(svc, "resolve_provider", lambda _: {"base_url": "https://upstream.example/v1", "protocol": "openai", "api_key": secret})
    event = {"type": "response.created", "response": {"id": secret, "model": "safe"}}
    chunks = [b"data: " + json.dumps(event).encode() + b"\n\n", b"data: [DONE]\n\n"]
    transport(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(chunks)))
    response = client.post("/api/routing/v1/responses", json={"model": "safe", "stream": True}, headers={"authorization": "Bearer " + token})
    assert secret not in response.text and "[DONE]" not in response.text
    assert usage_service.requests()["items"][0]["status"] == "unknown"
    with db() as conn:
        for table in ("usage_v2_attempts", "usage_v2_observations"):
            assert secret not in str([tuple(r) for r in conn.execute("SELECT * FROM " + table)])


def test_split_sse_metadata_is_validated_before_any_frame_bytes_leave(env):
    client, token, monkeypatch = env
    secret = "opaqueCredentialExactlyWithoutPrefix7654321"
    monkeypatch.setattr(svc, "resolve_provider", lambda _: {"base_url": "https://upstream.example/v1", "protocol": "openai", "api_key": secret})
    # The entire credential arrives before the SSE delimiter in a later chunk.
    chunks = [b'data: {"id":"' + secret.encode(), b'","choices":[]}\n\n', b'data: [DONE]\n\n']
    transport(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(chunks)))
    response = client.post("/api/routing/v1/chat/completions", json={"model": "safe", "stream": True}, headers={"authorization": "Bearer " + token})
    assert response.text == ""
    assert usage_service.requests()["items"][0]["status"] == "unknown"
