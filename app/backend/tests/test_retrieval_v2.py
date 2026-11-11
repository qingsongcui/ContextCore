import json
import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.database import create_schema, db, encode, now_iso, set_app_setting
from app.services import content_service, retrieval_service as retrieval


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "retrieval.db"))
    create_schema()
    with db() as conn:
        retrieval.ensure_schema(conn)
    return tmp_path


def memory(title="数据库", body="跨项目共享中文语义检索，保留原文证据", **kwargs):
    return content_service.create_content(title=title, content=body, **kwargs)


def test_chinese_fts_and_global_sharing_with_project_boost(store):
    with db() as conn:
        for ident in ("p1", "p2"):
            conn.execute("INSERT INTO projects (id,name,description,status,created_at,updated_at) VALUES (?,?,?,'active',?,?)",
                         (ident, ident, "", now_iso(), now_iso()))
    one = memory(project_id="p1")
    two = memory(title="另一个项目", project_id="p2")
    result = retrieval.search("中文语义检索", project_id="p1")
    assert {x["id"] for x in result["items"]} == {one["id"], two["id"]}
    assert result["items"][0]["id"] == one["id"]
    assert result["coverage"] == {"lexical": "fts5", "semantic": "not_requested", "scope": "global"}


def test_current_source_revalidation_rejects_stale_and_tombstone_index(store):
    item = memory()
    retrieval.refresh_index()
    # An out-of-date engine replica is not authoritative.
    with db() as conn:
        conn.execute("UPDATE retrieval_index SET content_version=999 WHERE entity_id=?", (item["id"],))
    # Regular search repairs mismatched versions.
    assert retrieval.search("共享")["items"][0]["version"] == 1
    with db() as conn:
        conn.execute("INSERT INTO tombstones(id,entity_type,dedupe_key,deleted_at) VALUES ('tb','content',?,?)",
                     (item["content_hash"], now_iso()))
    assert retrieval.search("共享")["items"] == []
    with pytest.raises(HTTPException) as error:
        retrieval.read_evidence(retrieval.evidence_id(item["id"], 1))
    assert error.value.status_code == 410


def test_paginated_evidence_update_invalidates_old_handle(store):
    item = memory(body="甲乙丙丁戊己庚辛壬癸")
    handle = retrieval.evidence_id(item["id"], 1)
    page = retrieval.read_evidence(handle, 2, 4)
    assert (page["text"], page["next_offset"], page["total_chars"]) == ("丙丁戊己", 6, 10)
    content_service.update_content(item["id"], content="新版本", expected_version=1)
    with pytest.raises(HTTPException) as error:
        retrieval.read_evidence(handle)
    assert error.value.status_code == 409
    assert retrieval.search("新版本")["items"][0]["version"] == 2


def test_independent_memory_survives_source_deletion_and_disabled_host(store):
    with db() as conn:
        conn.execute("""INSERT INTO conversations(id,agent,title,started_at,updated_at,created_at,messages)
            VALUES ('conv','codex','原聊天',?,?,?,?)""", (now_iso(), now_iso(), now_iso(), encode([])))
    item = memory(source_id="conv", source_kind="conversation", quote="共享")
    retrieval.refresh_index()
    prepared_before_delete = retrieval.assemble_context("共享")
    with db() as conn:
        conn.execute("DELETE FROM conversations WHERE id='conv'")
        conn.execute("UPDATE content_sources SET deleted=1,quote=NULL WHERE source_id='conv'")
    result = retrieval.search("共享")
    assert result["items"][0]["id"] == item["id"]
    assert result["items"][0]["sources"][0]["deleted"] is True
    with pytest.raises(HTTPException) as stale:
        retrieval.record_injection(prepared_before_delete["id"])
    assert stale.value.status_code in {409, 410}
    pack = retrieval.assemble_context("共享")
    assert "已删除" in pack["text"]
    assert retrieval.read_evidence(result["items"][0]["evidence_id"])["text"] == item["content"]


def test_protected_budget_fails_explicitly_without_truncation_or_ledger(store):
    memory()
    result = retrieval.assemble_context("", budget_tokens=5, protected=[{"id": "constraint", "text": "必须保留明确授权和精确资源标识"}])
    assert result["status"] == "insufficient_budget"
    assert result["shortfall_tokens"] > 0
    assert result["text"] == "" and result["entries"] == []
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM context_packs").fetchone()[0] == 0


def test_effective_constraints_are_protected_even_without_query_overlap(store):
    item = memory(title="精确参数", body="只能使用端口8420，未经许可不执行部署", category="decision")
    pack = retrieval.assemble_context("完全不相关问题", budget_tokens=300)
    assert pack["entries"][0]["id"] == item["id"]
    assert pack["entries"][0]["tier"] == "protected"
    assert item["content"] in pack["text"]
    too_small = retrieval.assemble_context("完全不相关问题", budget_tokens=5)
    assert too_small["status"] == "insufficient_budget" and too_small["text"] == ""
    assert too_small["shortfall_tokens"] > 0


def test_pack_budget_emission_epoch_version_dedup_and_no_body_ledger(store):
    item = memory(body="中文证据。" * 600)
    pack = retrieval.assemble_context("证据", budget_tokens=300, context_epoch="e1", consumer_id="codex")
    assert pack["status"] == "ready" and pack["estimated_tokens"] <= 300
    assert retrieval.estimate_tokens(pack["text"]) <= 300
    assert pack["entries"][0]["tier"] in {"passage", "reference"}
    assert f"[id: {item['id']} v1]" in pack["text"]
    # Preparation is not delivery. An unacknowledged pack is retryable.
    assert retrieval.assemble_context("证据", context_epoch="e1", consumer_id="codex")["entries"]
    assert retrieval.record_injection(pack["id"])["consumed"] == "unknown"
    repeated = retrieval.assemble_context("证据", context_epoch="e1", consumer_id="codex")
    assert not repeated["entries"]
    assert repeated["omitted"][0]["reason"] == "already_emitted_in_epoch"
    assert retrieval.assemble_context("证据", context_epoch="e2", consumer_id="codex")["entries"]
    content_service.update_content(item["id"], content="证据修改后立即生效", expected_version=1)
    latest = retrieval.assemble_context("证据", context_epoch="e1", consumer_id="codex")
    assert latest["entries"][0]["version"] == 2
    with db() as conn:
        for row in conn.execute("SELECT entries FROM context_packs"):
            assert "中文证据" not in row[0]


def test_delete_removes_index_vectors_relations_pack_ledger(store):
    retrieval.update_settings({"relation_enabled": True})
    a, b = memory(body="A明确引用B"), memory(title="B", body="B内容")
    retrieval.create_relation(a["id"], b["id"], "references", retrieval.evidence_id(a["id"], 1), "引用B")
    pack = retrieval.assemble_context("引用")
    retrieval.record_injection(pack["id"])
    content_service.delete_content(a["id"])
    assert retrieval.search("引用")["items"] == []
    assert retrieval.list_relations()["items"] == []
    with pytest.raises(HTTPException) as error:
        retrieval.record_injection(pack["id"])
    assert error.value.status_code == 410
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_index WHERE entity_id=?", (a["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM context_injections WHERE entity_id=?", (a["id"],)).fetchone()[0] == 0


def test_relation_requires_current_exact_quote_and_semantic_never_promotes_decision(store):
    a, b = memory(body="服务A调用服务B"), memory(title="B")
    handle = retrieval.evidence_id(a["id"], 1)
    with pytest.raises(HTTPException) as error:
        retrieval.create_relation(a["id"], b["id"], "calls", handle, "调用服务B")
    assert error.value.status_code == 409
    retrieval.update_settings({"relation_enabled": True})
    with pytest.raises(HTTPException) as error:
        retrieval.create_relation(a["id"], b["id"], "calls", handle, "假的原话")
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        retrieval.create_relation(a["id"], b["id"], "calls", handle, "调用服务B", semantic=True)
    assert error.value.status_code == 403
    with db() as conn:
        set_app_setting("compute_authorized", True, conn)
        set_app_setting("compute_host", "omp", conn)
    proposal = retrieval.create_relation(a["id"], b["id"], "calls", handle, "调用服务B", semantic=True)
    assert proposal["status"] == "unconfirmed"
    assert content_service.get_content(a["id"])["category"] == "knowledge"


def test_router_contract_and_settings_validation(store):
    from app.routers.retrieval import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    item = memory()
    status = client.get("/api/retrieval/status").json()
    assert status["scope"] == "global"
    assert status["settings"]["relation_enabled"] is False
    assert status["relations"]["enabled"] is False
    assert status["settings"]["semantic_enabled"] is False
    assert status["semantic"]["enabled"] is False
    assert client.patch("/api/retrieval/settings", json={"max_results": 0}).status_code == 422
    assert client.patch("/api/retrieval/settings", json={"semantic_enabled": True}).json()["semantic_enabled"] is True
    assert client.patch("/api/retrieval/settings", json={"relation_enabled": True}).json()["relation_enabled"] is True
    found = client.post("/api/retrieval/search", json={"query": "中文"}).json()["items"][0]
    assert found["id"] == item["id"]
    assert client.get(f"/api/retrieval/evidence/{found['evidence_id']}?limit=2").json()["next_offset"] == 2


def test_schema_extension_does_not_commit_callers_transaction(store):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO app_settings VALUES ('transaction_test','true',?)", (now_iso(),))
        retrieval.ensure_schema(conn)
        conn.rollback()
    with db() as conn:
        assert conn.execute("SELECT 1 FROM app_settings WHERE key='transaction_test'").fetchone() is None


def authorize_relation_compute():
    retrieval.update_settings({"relation_enabled": True})
    with db() as conn:
        set_app_setting("compute_authorized", True, conn)
        set_app_setting("compute_host", "omp", conn)
        conn.execute("UPDATE connections SET enabled=1,paused=0 WHERE id='omp'")

def test_relation_extraction_uses_safe_transport_and_keeps_proposals_unconfirmed(store, monkeypatch):
    from app import transport
    a, b = memory(body="服务A调用服务B"), memory(title="B", body="服务B接收请求")
    calls = []
    async def fake_run(host, payload, **kwargs):
        calls.append((host, payload, kwargs))
        return transport.DistillResult(status="success", memories=[{"category": "knowledge", "content": json.dumps(
            {"from_id": a["id"], "to_id": b["id"], "relation": "calls", "evidence_quote": "调用服务B"})}])
    monkeypatch.setattr(transport, "run_distill", fake_run)
    with pytest.raises(HTTPException) as error:
        asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))
    assert error.value.status_code == 403 and calls == []
    authorize_relation_compute()
    result = asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))
    assert result["status"] == "succeeded" and result["count"] == 1
    assert calls[0][0] == "omp" and calls[0][2]["allow_execution"] is True
    relation = retrieval.list_relations()["items"][0]
    assert relation["status"] == "unconfirmed" and relation["basis"] == "omp_extracted"
    assert content_service.get_content(a["id"])["version"] == 1
    again = asyncio.run(retrieval.extract_relations([b["id"], a["id"]]))
    assert again["duplicate"] and len(calls) == 1


def test_relation_extraction_rejects_unseen_ids_and_fake_quotes_atomically(store, monkeypatch):
    from app import transport
    a, b = memory(body="A引用B"), memory(title="B")
    authorize_relation_compute()
    async def fake_run(*args, **kwargs):
        return transport.DistillResult(status="success", memories=[{"category": "knowledge", "content": json.dumps(
            {"from_id": a["id"], "to_id": "unseen", "relation": "calls", "evidence_quote": "假的原文"})}])
    monkeypatch.setattr(transport, "run_distill", fake_run)
    result = asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))
    assert result["status"] == "failed" and result["error"] == "invalid_relation_output"
    assert retrieval.list_relations()["items"] == []


@pytest.mark.parametrize("race", ["source_edit", "revoke_regrant", "source_delete"])
def test_relation_extraction_rechecks_sources_and_authorization_after_model(store, monkeypatch, race):
    from app import transport
    a, b = memory(body="A引用B"), memory(title="B")
    authorize_relation_compute()
    async def fake_run(*args, **kwargs):
        if race == "source_edit":
            content_service.update_content(a["id"], content="A已不引用B", expected_version=1)
        elif race == "source_delete":
            content_service.delete_content(a["id"])
        else:
            with db() as conn:
                set_app_setting("compute_authorized", False, conn)
                set_app_setting("compute_authorized", True, conn)
        return transport.DistillResult(status="success", memories=[{"category": "knowledge", "content": json.dumps(
            {"from_id": a["id"], "to_id": b["id"], "relation": "references", "evidence_quote": "引用B"})}])
    monkeypatch.setattr(transport, "run_distill", fake_run)
    result = asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))
    assert result["status"] == "cancelled" and result["error"] == "snapshot_changed"
    assert retrieval.list_relations()["items"] == []


def test_unknown_relation_send_is_not_retried_after_regrant(store, monkeypatch):
    from app import transport
    a, b = memory(body="A引用B"), memory(title="B")
    authorize_relation_compute()
    calls = []
    async def fake_run(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("uncertain external result")
    monkeypatch.setattr(transport, "run_distill", fake_run)
    first = asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))
    assert first["status"] == "unknown"
    authorize_relation_compute()
    second = asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))
    assert second["status"] == "unknown" and second["duplicate"]
    assert len(calls) == 1


def test_hand_calculated_current_decision_set_changes_after_edit_and_delete(store):
    old = memory(title='GOLDENRETRIEVAL old', body='GOLDENRETRIEVAL previous decision', category='decision')
    current = memory(title='GOLDENRETRIEVAL current', body='GOLDENRETRIEVAL approved replacement', category='decision', supersedes_id=old['id'])
    other = memory(title='GOLDENRETRIEVAL reference', body='GOLDENRETRIEVAL background', category='background')
    assert {row['id'] for row in retrieval.search('GOLDENRETRIEVAL')['items']} == {current['id'], other['id']}
    assert content_service.get_content(old['id'])['status'] == 'historical'
    handle = retrieval.evidence_id(current['id'], current['version'])
    updated = content_service.update_content(current['id'], content='GOLDENRETRIEVAL revised evidence', expected_version=current['version'])
    with pytest.raises(HTTPException) as stale:
        retrieval.read_evidence(handle)
    assert stale.value.status_code == 409
    assert retrieval.read_evidence(retrieval.evidence_id(updated['id'], updated['version']))['text'] == 'GOLDENRETRIEVAL revised evidence'
    content_service.delete_content(current['id'], expected_version=updated['version'])
    assert {row['id'] for row in retrieval.search('GOLDENRETRIEVAL')['items']} == {other['id']}
    assert content_service.get_content(old['id'])['status'] == 'historical'


def test_relation_operations_before_consent_and_explicit_opt_in_lifecycle(store):
    # 1. Fresh state: both optional semantics and relations are disabled by default
    settings = retrieval.get_settings()
    assert settings["relation_enabled"] is False
    assert settings["semantic_enabled"] is False
    status = retrieval.get_status()
    assert status["relations"]["enabled"] is False
    assert status["semantic"]["enabled"] is False

    # 2. list_relations reports disabled status and empty items
    listing = retrieval.list_relations()
    assert listing == {"items": [], "count": 0, "status": "disabled"}

    # 3. create_relation before consent fails with 409
    a, b = memory(title="A", body="模块A依赖模块B"), memory(title="B", body="模块B实现核心逻辑")
    handle = retrieval.evidence_id(a["id"], 1)
    with pytest.raises(HTTPException) as err_create:
        retrieval.create_relation(a["id"], b["id"], "depends_on", handle, "依赖模块B")
    assert err_create.value.status_code == 409
    assert "disabled" in err_create.value.detail

    # 4. extract_relations before compute authorization fails with 403 (prod gate intact)
    with pytest.raises(HTTPException) as err_unauth:
        asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))
    assert err_unauth.value.status_code == 403

    # 5. Authorizing compute while relations remain disabled: extract_relations fails with 409
    with db() as conn:
        set_app_setting("compute_authorized", True, conn)
        set_app_setting("compute_host", "omp", conn)
        conn.execute("UPDATE connections SET enabled=1,paused=0 WHERE id='omp'")
    with pytest.raises(HTTPException) as err_rel_disabled:
        asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))
    assert err_rel_disabled.value.status_code == 409
    assert "disabled" in err_rel_disabled.value.detail

    # 6. Partial settings update does not accidentally enable relations
    retrieval.update_settings({"max_results": 25})
    assert retrieval.get_settings()["relation_enabled"] is False
    assert retrieval.get_settings()["max_results"] == 25

    # 7. Explicit opt-in enables relations
    updated = retrieval.update_settings({"relation_enabled": True})
    assert updated["relation_enabled"] is True
    assert retrieval.get_status()["relations"]["enabled"] is True

    # 8. Allowed write path succeeds once explicitly consented
    rel = retrieval.create_relation(a["id"], b["id"], "depends_on", handle, "依赖模块B")
    assert rel["status"] == "confirmed"
    active_list = retrieval.list_relations()
    assert active_list["status"] == "ready"
    assert len(active_list["items"]) == 1
    assert active_list["items"][0]["id"] == rel["id"]


def test_relation_invalidation_without_resurrection_on_source_edit_delete_rebuild_restart(store):
    retrieval.update_settings({"relation_enabled": True})
    a = memory(title="DocA", body="原文包含关联证据A")
    b = memory(title="DocB", body="原文包含关联证据B")
    c = memory(title="DocC", body="原文包含关联证据C")

    handle_a = retrieval.evidence_id(a["id"], 1)
    rel_ab = retrieval.create_relation(a["id"], b["id"], "references", handle_a, "关联证据A")
    assert len(retrieval.list_relations()["items"]) == 1

    # Source edit invalidates relation immediately
    content_service.update_content(a["id"], content="修改后的正文不再包含旧证据", expected_version=1)
    assert retrieval.list_relations()["items"] == []

    # Rebuilding / refreshing index does NOT resurrect the relation
    retrieval.refresh_index()
    assert retrieval.list_relations()["items"] == []
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_relations WHERE from_id=?", (a["id"],)).fetchone()[0] == 0

    # Now establish relation between B and C
    handle_b = retrieval.evidence_id(b["id"], 1)
    rel_bc = retrieval.create_relation(b["id"], c["id"], "links_to", handle_b, "关联证据B")
    assert len(retrieval.list_relations()["items"]) == 1

    # Source deletion invalidates and tombstones
    content_service.delete_content(b["id"], expected_version=1)
    assert retrieval.list_relations()["items"] == []

    # Rebuild index again: no resurrection
    retrieval.refresh_index()
    assert retrieval.list_relations()["items"] == []

    # Attempting to recreate relation with deleted/tombstoned content fails
    with pytest.raises(HTTPException) as err_recreate:
        retrieval.create_relation(b["id"], c["id"], "links_to", handle_b, "关联证据B")
    assert err_recreate.value.status_code in {409, 410}

    # Simulated service restart with fresh connection/schema check does not bring back records
    with db() as conn:
        retrieval.ensure_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM retrieval_relations").fetchone()[0] == 0


def test_relation_extract_disabled_while_running_fences_commit(store, monkeypatch):
    from app import transport
    authorize_relation_compute()
    a, b = memory(body="服务A实时通信服务B"), memory(title="B", body="服务B接收消息")

    async def fake_run_and_disable(host, payload, **kwargs):
        # While the external compute transport is running, relation indexing is disabled by user
        retrieval.update_settings({"relation_enabled": False})
        return transport.DistillResult(
            status="success",
            memories=[{"category": "knowledge", "content": json.dumps(
                {"from_id": a["id"], "to_id": b["id"], "relation": "communicates_with", "evidence_quote": "通信服务B"}
            )}]
        )

    monkeypatch.setattr(transport, "run_distill", fake_run_and_disable)
    result = asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))
    assert result["status"] == "cancelled"
    assert result["error"] == "snapshot_changed"
    # Ensure no relation proposal leaked into the database
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_relations").fetchone()[0] == 0


def test_relation_extract_propagates_programmer_error_with_failed_bookkeeping(store, monkeypatch):
    from app import transport
    authorize_relation_compute()
    a, b = memory(body="服务A调用服务B"), memory(title="B", body="服务B接收请求")

    async def buggy_transport(host, payload, **kwargs):
        raise TypeError("unhandled programmer bug inside runner")

    monkeypatch.setattr(transport, "run_distill", buggy_transport)
    # Programmer exception must propagate, NOT be swallowed into status="unknown"
    with pytest.raises(TypeError, match="unhandled programmer bug"):
        asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))

    # Safe explicit bookkeeping: state is recorded as failed, not unknown or succeeded
    with db() as conn:
        attempt = conn.execute("SELECT * FROM relation_extractions").fetchone()
        assert attempt is not None
        assert attempt["state"] == "failed"
        assert attempt["error_code"] == "internal_error"
        assert conn.execute("SELECT COUNT(*) FROM retrieval_relations").fetchone()[0] == 0


def test_relation_extract_typed_distill_execution_error_and_post_run_failure(store, monkeypatch):
    from app import transport
    authorize_relation_compute()
    a, b = memory(body="服务A调用服务B"), memory(title="B", body="服务B接收请求")

    # 1. DistillExecutionError with kind='transport', send_started=True, result_received=False => status="unknown"
    async def transport_interrupted(host, payload, **kwargs):
        err = transport.DistillExecutionError("interrupted after send")
        err.kind = "transport"
        err.send_started = True
        err.result_received = False
        raise err

    monkeypatch.setattr(transport, "run_distill", transport_interrupted)
    res_interrupted = asyncio.run(retrieval.extract_relations([a["id"], b["id"]]))
    assert res_interrupted["status"] == "unknown"
    with db() as conn:
        row = conn.execute("SELECT * FROM relation_extractions WHERE id=?", (res_interrupted["id"],)).fetchone()
        assert row["state"] == "unknown"
        assert row["error_code"] == "outcome_unknown"

    # 2. DistillExecutionError with kind='transport', send_started=False => status="failed", transport_failed (not unknown)
    async def pre_send_failure(host, payload, **kwargs):
        err = transport.DistillExecutionError("pre-send failure")
        err.kind = "transport"
        err.send_started = False
        err.result_received = False
        raise err

    monkeypatch.setattr(transport, "run_distill", pre_send_failure)
    res_presend = asyncio.run(retrieval.extract_relations([a["id"], b["id"]], retry=True))
    assert res_presend["status"] == "failed"
    assert res_presend["error"] == "transport_failed"
    with db() as conn:
        row = conn.execute("SELECT * FROM relation_extractions WHERE id=?", (res_presend["id"],)).fetchone()
        assert row["state"] == "failed"
        assert row["error_code"] == "transport_failed"

    # 3. DistillExecutionError with kind='implementation' => re-raises and books state="failed"
    async def implementation_bug(host, payload, **kwargs):
        err = transport.DistillExecutionError("internal implementation bug")
        err.kind = "implementation"
        raise err

    monkeypatch.setattr(transport, "run_distill", implementation_bug)
    with pytest.raises(transport.DistillExecutionError, match="internal implementation bug"):
        asyncio.run(retrieval.extract_relations([a["id"], b["id"]], retry=True))

    # 4. CancelledError after invoke => conservative unknown crashfence and re-raises
    async def cancelled_send(host, payload, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(transport, "run_distill", cancelled_send)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(retrieval.extract_relations([a["id"], b["id"]], retry=True))
    with db() as conn:
        latest = conn.execute("SELECT * FROM relation_extractions ORDER BY created_at DESC LIMIT 1").fetchone()
        assert latest["state"] == "unknown"
        assert latest["error_code"] == "outcome_unknown"

    # 4b. CancelledError with explicit send_started=False => determinate pre-send cancellation
    async def cancelled_presend(host, payload, **kwargs):
        err = asyncio.CancelledError()
        err.send_started = False
        err.result_received = False
        raise err

    monkeypatch.setattr(transport, "run_distill", cancelled_presend)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(retrieval.extract_relations([a["id"], b["id"]], retry=True))
    with db() as conn:
        latest = conn.execute("SELECT * FROM relation_extractions ORDER BY created_at DESC LIMIT 1").fetchone()
        assert latest["state"] == "cancelled"
        assert latest["error_code"] == "task_cancelled"

    # 5. Completed CLI stdout with malformed/failed result => status="failed", error="malformed_output" (not unknown)
    async def malformed_result(host, payload, **kwargs):
        return transport.DistillResult(
            status="failed",
            error="Host CLI returned invalid structured output",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    monkeypatch.setattr(transport, "run_distill", malformed_result)
    res_malformed = asyncio.run(retrieval.extract_relations([a["id"], b["id"]], retry=True))
    assert res_malformed["status"] == "failed"
    assert res_malformed["error"] == "malformed_output"
    assert res_malformed["automatic_retry"] is False
    with db() as conn:
        row = conn.execute("SELECT * FROM relation_extractions WHERE id=?", (res_malformed["id"],)).fetchone()
        assert row["state"] == "failed"
        assert row["error_code"] == "malformed_output"

