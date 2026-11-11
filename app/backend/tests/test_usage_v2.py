import json
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from fastapi import FastAPI
from app.database import create_schema, db
from app.services import usage_service as svc
from app.routers.usage import router


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "usage.db"))
    create_schema(tmp_path / "usage.db")
    svc.ensure_schema()
    return svc


def observation(key="one", **patch):
    return {"origin": "native_log", "observation_id": key, "provider_id": "p", "model": "m", "started_at": "2026-10-23T16:01:00+00:00", "raw_usage": {"input_tokens": 1000, "output_tokens": 100}, **patch}


def test_replay_atomic_conflict_and_no_body_storage(ledger):
    item = observation()
    assert ledger.ingest([item])["accepted"] == 1
    assert ledger.ingest([item])["duplicates"] == 1
    with pytest.raises(HTTPException) as error:
        ledger.ingest([observation("second"), observation(raw_usage={"input_tokens": 10})])
    assert error.value.status_code == 409
    assert ledger.requests()["total"] == 1
    with pytest.raises(HTTPException):
        ledger.ingest([observation("unsafe", request_body="secret")])
    with db() as conn:
        assert "secret" not in json.dumps([tuple(r) for r in conn.execute("SELECT * FROM usage_v2_observations")])


def test_stable_request_correlation_and_attempt_accounting(ledger):
    first = ledger.ingest([observation(upstream_request_id="up-1", logical_request_id="logical", attempt_id="a")])
    ledger.ingest([observation("proxy", origin="proxy", upstream_request_id="up-1", status="failed", raw_usage={"prompt_tokens": 1000, "completion_tokens": 100})])
    ledger.ingest([observation("fallback", logical_request_id="logical", attempt_id="b", provider_id="p2", status="succeeded")])
    total = ledger.summary()
    assert total["logical_requests"] == 1
    assert total["attempts"] == 2
    assert total["known_tokens"] == 2200
    assert total["status_counts"]["failed"] == 1
    assert ledger.requests()["items"][-1]["observations"] == 2
    assert ledger.ingest([observation("uncorrelated", provider_id="p2")])["accepted"] == 1
    assert ledger.requests()["total"] == 3  # Never merge merely by text/model/time.


def test_decimal_cache_semantics_and_price_snapshot(ledger):
    ledger.add_price({"provider_id": "p", "model": "m", "effective_from": "2026-01-01T00:00:00Z", "currency": "USD", "input_per_million": "2.50", "output_per_million": "10", "cache_read_per_million": "0.50", "cache_write_per_million": "3"})
    ledger.ingest([observation(raw_usage={"input_tokens": 1000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 200}, "cache_write_tokens": 0, "output_tokens_details": {"reasoning_tokens": 40}})])
    row = ledger.requests()["items"][0]
    assert row["cost"]["amount"] == "0.0031"
    assert row["usage"]["reasoning_tokens"] == 40
    assert ledger.summary()["known_tokens"] == 1100  # Reasoning/cache aren't added twice.
    price_id = row["cost"]["price_snapshot"]["id"]
    ledger.add_price({"provider_id": "p", "model": "m", "effective_from": "2026-01-01T00:00:00Z", "input_per_million": "999"})
    ledger.ingest([observation("supplement", attempt_id=row["id"], raw_usage={"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 200, "cache_write_tokens": 0})])
    assert ledger.requests()["items"][0]["cost"]["price_snapshot"]["id"] == price_id
    usage = ledger.normalize_usage({"input_tokens": 800, "output_tokens": 100, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 0}, "anthropic")
    assert usage["input_tokens"] == 1000 and usage["uncached_input_tokens"] == 800


def test_unknown_missing_usage_and_cache_not_zero(ledger):
    ledger.add_price({"provider_id": "p", "model": "m", "input_per_million": "2", "output_per_million": "10", "cache_read_per_million": "0.2", "effective_from": "2026-01-01T00:00:00Z"})
    ledger.ingest([observation(), observation("missing", raw_usage={})])
    data = ledger.summary()
    assert data["input_tokens"] is None and data["output_tokens"] is None
    assert data["unknown_usage_count"] == 1
    assert data["unknown_cost_count"] == 2
    assert data["cost_by_currency"] == {}
    assert ledger.coverage()["all_host_usage"] == "unknown"


def test_cumulative_reset_replay_and_timezone_filters(ledger):
    common = {"source_id": "host-session", "cumulative": True}
    ledger.ingest([observation("s1", sequence=1, **common)])
    ledger.ingest([observation("s2", sequence=2, raw_usage={"input_tokens": 1200, "output_tokens": 120}, **common)])
    ledger.ingest([observation("s3", sequence=3, raw_usage={"input_tokens": 10, "output_tokens": 1}, **common)])
    assert ledger.summary()["known_tokens"] == 1331
    assert ledger.ingest([observation("s3", sequence=3, raw_usage={"input_tokens": 10, "output_tokens": 1}, **common)])["duplicates"] == 1
    assert ledger.summary()["heatmap"][0]["date"] == "2026-10-24"
    assert ledger.summary(tz="UTC")["heatmap"][0]["date"] == "2026-10-23"
    assert ledger.requests({"provider_id": "other"})["total"] == 0
    with pytest.raises(HTTPException):
        ledger.summary(tz="No/SuchZone")


def test_real_timing_rules_single_chunk_and_heartbeat(ledger):
    ledger.ingest([observation(origin="proxy", first_content_at="2026-10-23T16:01:01Z", last_content_at="2026-10-23T16:01:03Z", ended_at="2026-10-23T16:01:04Z", content_chunks=2)])
    data = ledger.summary()["performance"]
    assert data["ttft_ms"]["p50"] == 1000
    assert data["output_tokens_per_second"]["mean"] == 50
    ledger.ingest([observation("single", origin="proxy", first_content_at="2026-10-23T16:01:01Z", last_content_at="2026-10-23T16:01:01Z", content_chunks=1)])
    assert ledger.summary()["performance"]["output_tokens_per_second"]["samples"] == 1


def test_usage_api_pagination_and_explicit_batch(ledger):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.post("/api/usage/ingest", json={"observations": [observation()]}).status_code == 200
    assert client.get("/api/usage/requests?limit=1&offset=0").json()["total"] == 1
    assert client.post("/api/usage/ingest", json={"path": "/private/history"}).status_code == 422
    assert client.get("/api/usage/requests?limit=9999").status_code == 422

@pytest.mark.parametrize("order", [("start", "native", "finish"), ("native", "start", "finish"), ("start", "finish", "native")])
def test_late_upstream_receipts_merge_any_order(ledger, order):
    rows = {
        "start": observation("proxy-start", origin="proxy", logical_request_id="logical", attempt_id="a", status="running", raw_usage={}),
        "native": observation("native", upstream_request_id="X", attempt_id="native-a", raw_usage={"input_tokens": 100, "output_tokens": 10}),
        "finish": observation("proxy-finish", origin="proxy", logical_request_id="logical", attempt_id="a", upstream_request_id="X", raw_usage={"input_tokens": 100, "output_tokens": 10}),
    }
    for step in order:
        ledger.ingest([rows[step]])
    summary = ledger.summary()
    assert summary["attempts"] == summary["logical_requests"] == 1
    assert summary["input_tokens"] == 100
    assert ledger.requests()["items"][0]["observations"] == 3
    assert ledger.requests()["items"][0]["origins"] == ["native_log", "proxy"]
    for step in order:
        assert ledger.ingest([rows[step]])["duplicates"] == 1
    assert ledger.requests()["total"] == 1


def test_native_parsers_are_pure_replay_safe_and_cache_aware(ledger):
    messages = [{"type": "event_msg", "timestamp": "2026-10-24T00:00:00Z", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10}}}}]
    events = ledger.extract_usage_from_messages(messages, host_id="codex", source_id="session")
    assert len(events) == 1 and events[0]["cumulative"] is True
    ledger.ingest(events)
    assert ledger.ingest(events)["duplicates"] == 1
    assert ledger.requests()["items"][0]["usage"]["cache_read_tokens"] == 20
    claude = [{"uuid": "id", "timestamp": "2026-10-24T00:00:00Z", "message": {"id": "resp", "usage": {"input_tokens": 20, "cache_read_input_tokens": 10, "output_tokens": 2}, "model": "claude"}}]
    parsed = ledger.extract_usage_from_messages(claude, host_id="claude_code", source_id="s")
    ledger.ingest(parsed)
    assert ledger.requests()["items"][0]["usage"]["input_tokens"] in (30, 100)


def test_schema_ensure_does_not_commit_caller_transaction(ledger):
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES ('rollback-marker','true','now')")
            ledger.ensure_schema(conn)
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    with db() as conn:
        assert conn.execute("SELECT * FROM app_settings WHERE key='rollback-marker'").fetchone() is None


def test_claude_partial_usage_same_message_without_provider_merges(ledger):
    rows = [{"uuid": f"u{i}", "timestamp": "2026-10-24T00:00:00Z", "message": {"id": "same-message", "usage": {"input_tokens": 20, "output_tokens": i}, "model": "claude"}} for i in (1, 2)]
    events = ledger.extract_usage_from_messages(rows, host_id="claude-code", source_id="source")
    ledger.ingest(events)
    assert ledger.summary()["attempts"] == 1
    assert ledger.summary()["output_tokens"] == 2


def test_mutable_native_message_usage_is_new_revision_same_attempt(ledger):
    def row(out):
        return {"info": {"id": "msg", "time": {"created": 1792800000000}, "tokens": {"input": 10, "output": out, "cache": {"read": 0, "write": 0}}}}
    ledger.ingest(ledger.extract_usage_from_messages([row(1)], host_id="opencode", source_id="session"))
    ledger.ingest(ledger.extract_usage_from_messages([row(4)], host_id="opencode", source_id="session"))
    assert ledger.summary()["attempts"] == 1
    assert ledger.summary()["output_tokens"] == 4


def test_partial_proxy_cannot_overwrite_complete_native_receipt(ledger):
    ledger.ingest([observation("native", upstream_request_id="same", raw_usage={"input_tokens": 100, "output_tokens": 20})])
    ledger.ingest([observation("proxy-partial", origin="proxy", upstream_request_id="same", status="unknown", raw_usage={"input_tokens": 100, "output_tokens": 0})])
    assert ledger.summary()["attempts"] == 1 and ledger.summary()["output_tokens"] == 20
    assert ledger.requests()["items"][0]["status"] == "succeeded"


def test_missing_cache_counters_do_not_report_all_input_as_uncached(ledger):
    usage = ledger.normalize_usage({"input_tokens": 1000, "output_tokens": 100}, "openai")
    assert usage["input_tokens"] == 1000
    assert usage["cache_read_tokens"] is None
    assert usage["cache_write_tokens"] is None
    assert usage["uncached_input_tokens"] is None

