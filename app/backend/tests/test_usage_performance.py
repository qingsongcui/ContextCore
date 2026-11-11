"""Safe synthetic-only usage regressions; opt-in large run:

SENRELAY_USAGE_PERF=1 nice -n 19 .venv/bin/pytest -s -q tests/test_usage_performance.py

The 100k fixture is inserted in sleeping 500-row batches, never through an old
unindexed ingestion path. No real data directory, history, service or API is used.
"""
import json
import os
import time
import tracemalloc
from contextlib import contextmanager

import pytest

from app.database import create_schema, db, encode
from app.services import usage_service as svc


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "synthetic-usage.db"))
    create_schema(tmp_path / "synthetic-usage.db")
    svc.ensure_schema()
    return tmp_path / "synthetic-usage.db"


def item(key="one", **patch):
    return {"origin": "native_log", "observation_id": key, "provider_id": "p", "model": "m", "host_id": "h", "started_at": "2026-10-23T16:01:00Z", "raw_usage": {"input_tokens": 10, "output_tokens": 2}, **patch}


def trace_sql(monkeypatch):
    statements = []
    @contextmanager
    def traced_db():
        with db() as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(svc, "db", traced_db)
    return statements


def test_ingest_lookup_and_page_sort_use_indexes(ledger):
    with db() as conn:
        plans = {"observation": conn.execute("EXPLAIN QUERY PLAN SELECT data FROM usage_v2_observations WHERE attempt_id=? ORDER BY rowid", ("a",)).fetchall(),
                 "page": conn.execute("EXPLAIN QUERY PLAN SELECT data FROM usage_v2_attempts ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?", (50, 0)).fetchall(),
                 "host_page": conn.execute("EXPLAIN QUERY PLAN SELECT data FROM usage_v2_attempts WHERE host_id=? ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?", ("h", 50, 0)).fetchall(),
                 "logical_cleanup": conn.execute("EXPLAIN QUERY PLAN SELECT 1 FROM usage_v2_attempts WHERE logical_request_id=?", ("r",)).fetchall()}
    details = {key: " ".join(row["detail"] for row in rows) for key, rows in plans.items()}
    assert "usage_v2_observation_attempt" in details["observation"] and "SEARCH" in details["observation"]
    assert "usage_v2_attempt_page" in details["page"] and "TEMP B-TREE" not in details["page"]
    assert "usage_v2_attempt_host_page" in details["host_page"] and "TEMP B-TREE" not in details["host_page"]
    assert "usage_v2_attempt_logical" in details["logical_cleanup"]


def test_requests_decode_only_requested_page(ledger, monkeypatch):
    svc.ingest([item(str(i)) for i in range(40)])
    decoded = []
    original = svc.decode
    def counting(value, default=None):
        decoded.append(value)
        return original(value, default)
    monkeypatch.setattr(svc, "decode", counting)
    sql = trace_sql(monkeypatch)
    page = svc.requests(limit=7, offset=11)
    assert page["total"] == 40 and len(page["items"]) == len(decoded) == 7
    assert any("LIMIT 7 OFFSET 11" in query for query in sql)


def test_summary_compact_projection_reuses_coverage_and_committed_revision(ledger, monkeypatch):
    svc.ingest([item()])
    sql = trace_sql(monkeypatch)
    monkeypatch.setattr(svc, "decode", lambda *_: (_ for _ in ()).throw(AssertionError("Full attempt JSON must not be decoded by statistics")))
    first = svc.summary()
    assert first["known_tokens"] == 12
    stats_queries = lambda: [query for query in sql if "SELECT logical_request_id,started_at,status" in query]
    assert len(stats_queries()) == 1
    assert svc.coverage() == first["coverage"]
    svc.summary()
    assert len(stats_queries()) == 1
    assert not any("SELECT json_extract(data,'$.usage_complete','$.origins')" in query for query in sql)
    # An external SQL writer is also observed, not only service.ingest().
    with db() as conn:
        row = conn.execute("SELECT id,data FROM usage_v2_attempts").fetchone()
        data = json.loads(row["data"])
        data["usage"]["input_tokens"] = 15
        conn.execute("UPDATE usage_v2_attempts SET data=? WHERE id=?", (encode(data), row["id"]))
    assert svc.summary()["known_tokens"] == 17 and len(stats_queries()) == 2
    try:
        with db() as conn:
            conn.execute("UPDATE usage_v2_attempts SET status='failed'")
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    svc.summary()
    assert len(stats_queries()) == 2  # rolled-back writes never invalidate.
    with db() as conn:
        conn.execute("DELETE FROM usage_v2_attempts")
    assert svc.coverage()["observed_attempts"] == 0
    assert svc.summary()["attempts"] == 0


@pytest.mark.skipif(os.environ.get("SENRELAY_USAGE_PERF") != "1", reason="100k performance run is explicit and low-priority only")
def test_100k_synthetic_usage_bounded_queries(ledger, monkeypatch):
    # Do not raise priority if the parent has already lowered it.
    if hasattr(os, "getpriority") and hasattr(os, "setpriority"):
        os.setpriority(os.PRIO_PROCESS, 0, 19)
    n = 100_000
    stamp = "2026-10-23T16:01:00+00:00"
    started = time.perf_counter()
    for begin in range(0, n, 500):
        requests, attempts, observations = [], [], []
        for index in range(begin, begin + 500):
            aid, rid = f"perf-{index:06}", f"request-{index:06}"
            inp = None if index % 10 == 0 else 10
            data = {"id": aid, "logical_request_id": rid, "origin": "native_log", "origins": ["native_log"], "started_at": stamp, "status": "succeeded", "usage": {"input_tokens": inp, "output_tokens": 2}, "usage_complete": inp is not None,
                    "cost": {"amount": "0.000003" if inp is not None else None, "currency": "USD", "price_snapshot": {"id": "fixed", "currency": "USD", "input_per_million": "0.25", "output_per_million": "0.25"}},
                    "performance": {"ttft_ms": 2.0, "end_to_end_ms": 20.0, "output_tokens_per_second": 3.0},
                    "observation_refs": [{"origin": "native_log", "observation_id": f"event-{index}-{j}"} for j in range(3)]}
            requests.append((rid, stamp))
            attempts.append((aid, rid, "p", "m", "h", None, None, None, stamp, "succeeded", encode(data)))
            raw = svc._safe_observation(item(str(index), attempt_id=aid))
            observations.append(("native_log", str(index), aid, "synthetic-hash", encode(raw)))
        with db() as conn:
            conn.executemany("INSERT INTO usage_v2_requests VALUES (?,?)", requests)
            conn.executemany("INSERT INTO usage_v2_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)", attempts)
            conn.executemany("INSERT INTO usage_v2_observations VALUES (?,?,?,?,?)", observations)
        time.sleep(.003)
    build_seconds = time.perf_counter() - started
    with db() as conn:
        plans = {"observation": [r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN SELECT data FROM usage_v2_observations WHERE attempt_id=? ORDER BY rowid", ("perf-099999",))],
                 "page": [r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN SELECT data FROM usage_v2_attempts ORDER BY started_at DESC,id DESC LIMIT 50 OFFSET 50000")],
                 "host_page": [r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN SELECT data FROM usage_v2_attempts WHERE host_id='h' ORDER BY started_at DESC,id DESC LIMIT 50 OFFSET 50000")]}
    original = svc.decode
    decoded = []
    def counting(value, default=None):
        decoded.append(1)
        return original(value, default)
    monkeypatch.setattr(svc, "decode", counting)
    tracemalloc.start()
    then = time.perf_counter()
    page = svc.requests(limit=50, offset=50000)
    page_seconds = time.perf_counter() - then
    page_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert page["total"] == n and len(page["items"]) == len(decoded) == 50
    decoded.clear()
    then = time.perf_counter()
    svc.summary()
    cold_summary_seconds = time.perf_counter() - then
    with svc._CACHE_LOCK:
        svc._AGGREGATES.clear()
    tracemalloc.start()
    then = time.perf_counter()
    summary = svc.summary()
    summary_seconds = time.perf_counter() - then
    summary_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert summary["attempts"] == n and summary["logical_requests"] == n
    assert summary["input_tokens"] is None and summary["unknown_usage_count"] == 10000
    assert summary["known_tokens"] == 1_100_000
    assert summary["cost_by_currency"] == {"USD": "0.270000"}
    assert decoded == []  # No full records loaded for statistics.
    then = time.perf_counter()
    assert svc.coverage() == summary["coverage"]
    coverage_seconds = time.perf_counter() - then
    then = time.perf_counter()
    svc.summary()
    warm_summary_seconds = time.perf_counter() - then
    monkeypatch.setattr(svc, "decode", original)
    then = time.perf_counter()
    result = svc.ingest([item("new-receipt", attempt_id="perf-099999")])
    ingest_seconds = time.perf_counter() - then
    assert result["accepted"] == 1
    assert "SEARCH" in " ".join(plans["observation"])
    assert all("TEMP B-TREE" not in plan for plan in plans["page"] + plans["host_page"])
    assert page_peak < 4 * 1024 * 1024 and summary_peak < 40 * 1024 * 1024
    # Hard bounds target catastrophic regressions; timings are reported rather
    # than tight wall-clock assertions on the user's shared computer.
    print("USAGE_PERF " + json.dumps({"rows": n, "batch_size": 500, "nice": os.getpriority(os.PRIO_PROCESS, 0), "build_seconds": round(build_seconds, 4), "page_seconds": round(page_seconds, 4), "page_decoded_rows": 50, "page_peak_mib": round(page_peak / 1048576, 3), "cold_summary_seconds": round(cold_summary_seconds, 4), "summary_seconds_tracemalloc": round(summary_seconds, 4), "summary_full_records_decoded": 0, "summary_peak_mib": round(summary_peak / 1048576, 3), "coverage_after_summary_seconds": round(coverage_seconds, 4), "warm_summary_seconds": round(warm_summary_seconds, 4), "ingest_existing_attempt_seconds": round(ingest_seconds, 4), "query_plans": plans}, ensure_ascii=False))
