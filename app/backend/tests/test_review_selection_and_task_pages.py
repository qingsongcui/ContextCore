"""Consumer-visible regressions for stale selection and task page navigation."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.database import create_schema, db, now_iso
from app.routers.working_tasks import router
from app.services import conversation_service as conversations
from app.services import handoff_service as relay
from app.services import project_service as projects
from app.services import working_memory_service as tasks


@pytest.fixture(autouse=True)
def schema():
    create_schema()


def chat(project_id, suffix, minute):
    return conversations.upsert_conversation(
        agent="antigravity", project_id=project_id, title=f"Synthetic chat {suffix}",
        external_id=f"antigravity:review-{suffix}", updated_at=f"2026-11-12T10:{minute:02}:00Z",
        messages=[{"role": "user", "content": f"Investigate distinct scenario {suffix}"}],
    )


@pytest.mark.parametrize("invalid", ["deleted", "expired", "missing", "other_consumer", "unknown_index"])
def test_invalid_number_never_reranks_or_refreshes_another_source(invalid, monkeypatch):
    project = projects.create_project(name="Selection isolation")
    original = chat(project["id"], "original", 1)
    relay.discover_relay_conversations(consumer="omp", project_id=project["id"], sync_first=False)
    replacement = chat(project["id"], "newer", 2)
    assert replacement["id"] != original["id"]
    consumer, index = "omp", "1"
    if invalid == "deleted":
        conversations.delete_conversation(original["id"])
    elif invalid == "expired":
        with db() as conn:
            conn.execute("UPDATE relay_discovery_snapshots SET updated_at='2000-01-01T00:00:00+00:00'")
    elif invalid == "missing":
        with db() as conn:
            conn.execute("DELETE FROM relay_discovery_snapshots")
    elif invalid == "other_consumer":
        consumer = "codex"
    else:
        index = "2"

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid selection must not re-list or read another host source")

    monkeypatch.setattr(relay, "discover_relay_conversations", forbidden)
    from app.services import connection_service
    monkeypatch.setattr(connection_service, "sync_conversation_for_relay", forbidden)
    with pytest.raises(HTTPException) as rejected:
        relay.relay_conversation(index, consumer=consumer, project_id=project["id"])
    assert rejected.value.status_code == (410 if invalid == "deleted" else 409)
    assert rejected.value.detail["must_not_execute"] is True
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0


def test_exact_id_remains_usable_after_numbered_list_expires():
    project = projects.create_project(name="Exact selection")
    original = chat(project["id"], "exact", 1)
    relay.discover_relay_conversations(consumer="omp", project_id=project["id"], sync_first=False)
    with db() as conn:
        conn.execute("UPDATE relay_discovery_snapshots SET updated_at='2000-01-01T00:00:00+00:00'")
    result = relay.relay_conversation(original["id"], consumer="omp", sync_first=False)
    assert result["conversation_id"] == original["id"]
    assert result["status"] == "loaded"
    assert result["must_not_execute"] is True


def seed_task_page(project_id, status, count):
    now = now_iso()
    with db() as conn:
        for i in range(count):
            conn.execute(
                """INSERT INTO working_tasks(id,project_id,title,normalized_title,status,
                    execution_mode,version,created_at,updated_at)
                    VALUES(?,?,?,?,?,'approved',1,?,?)""",
                (f"{status}-{i:03}", project_id, f"{status} task {i}", f"{status}{i}", status, now, now),
            )


@pytest.mark.parametrize("status,group", [("active", "active"), ("completed", "history")])
def test_position_endpoint_finds_task_after_200_in_same_order_as_list(status, group):
    project = projects.create_project(name="Large task archive")
    seed_task_page(project["id"], status, 205)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        result = client.get(f"/api/working-tasks/{status}-000/position", params={"project_id": project["id"], "limit": 50})
        assert result.status_code == 200
        page = result.json()
        assert page["group"] == group
        assert page["offset"] == 200
        params = {"project_id": project["id"], "limit": 50, "offset": page["offset"]}
        params.update({"active_only": True} if group == "active" else {"status": "history"})
        listed = client.get("/api/working-tasks", params=params).json()
        assert f"{status}-000" in [task["id"] for task in listed]
        assert len(listed) == 5
        assert client.get("/api/working-tasks/summary", params={"project_id": project["id"]}).json()[group] == 205
        other = projects.create_project(name="Different project")
        assert client.get(f"/api/working-tasks/{status}-000/position", params={"project_id": other["id"]}).status_code == 404

def test_backfill_replaces_untitled_session_with_human_digest_and_excludes_omp_subagents(tmp_path):
    import json
    from app.adapters import read_sessions
    from app.database import encode

    project = projects.create_project(name="Dashboard Prototype")
    with db() as conn:
        conn.execute(
            """INSERT INTO conversations
               (id,external_id,agent,title,project_id,source_path,started_at,updated_at,messages,process_summary,processing_status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "conv_legacy_omp",
                "omp:01a1-main",
                "omp",
                "Untitled Session",
                project["id"],
                "/tmp/2026-11-12T05-33-34-149Z_01a1ce3f-4045-7251-90f5-ca2d0ae2d73a.jsonl",
                "2026-11-12T05:33:34Z",
                "2026-11-12T07:34:17Z",
                encode([
                    {"role": "user", "content": "<system-reminder>Today: 2026-11-12</system-reminder>全面了解一下项目"},
                    {"role": "assistant", "content": "已梳理目录结构。"},
                    {"role": "user", "content": "把商品卡监测的计费单位改成人民币分厘整数账本"},
                    {"role": "assistant", "content": "12 项全部完成，桌面与移动原型均改为人民币分厘账本，浏览器实测通过。"},
                ]),
                "- **bash** [pending]: Called tool bash({'command': 'ls -la'})",
                "pending",
                "2026-11-12T05:33:34Z",
            ),
        )
        conn.execute(
            """INSERT INTO conversations
               (id,external_id,agent,title,project_id,source_path,started_at,updated_at,messages,process_summary,processing_status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "conv_legacy_subagent",
                "omp:01a1-sub",
                "omp",
                "Untitled Session",
                project["id"],
                "/tmp/2026-11-12T05-33-34-149Z_01a1ce3f-4045-7251-90f5-ca2d0ae2d73a/MobileWorker.jsonl",
                "2026-11-12T05:50:00Z",
                "2026-11-12T06:10:30Z",
                encode([{"role": "user", "content": "Complete assignment thoroughly: # Target mobile/**"}]),
                "",
                "pending",
                "2026-11-12T05:50:00Z",
            ),
        )

    remaining = conversations.backfill_relay_index_batch(limit=50)
    assert remaining == 0
    listed = relay.discover_relay_conversations(consumer="codex", project_id=project["id"], sync_first=False)
    assert [c["id"] for c in listed["candidates"]] == ["conv_legacy_omp"]
    cand = listed["candidates"][0]
    assert cand["title"] == "把商品卡监测的计费单位改成人民币分厘整数账本"
    assert "聊了什么：" in cand["brief_summary"]
    assert "12 项全部完成" in cand["brief_summary"]
    assert "Called tool bash" not in cand["brief_summary"]

    omp_root = tmp_path / "omp-sessions"
    sub_dir = omp_root / "2026-11-12T05-33-34-149Z_01a1ce3f-4045-7251-90f5-ca2d0ae2d73a"
    sub_dir.mkdir(parents=True)
    (omp_root / "2026-11-12T05-33-34-149Z_01a1ce3f-4045-7251-90f5-ca2d0ae2d73a.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "title", "v": 1, "title": ""}),
            json.dumps({"type": "session", "version": 3, "id": "main-1", "timestamp": "2026-11-12T05:33:34Z"}),
            json.dumps({"type": "message", "id": "m1", "parentId": None, "message": {"role": "user", "content": "重构计费卡片样式"}}),
        ]) + "\n",
        encoding="utf-8",
    )
    (sub_dir / "WorkerChild.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "title", "v": 1, "title": ""}),
            json.dumps({"type": "session", "version": 3, "id": "sub-1", "parentSession": "main-1", "timestamp": "2026-11-12T05:35:00Z"}),
            json.dumps({"type": "message", "id": "s1", "parentId": None, "message": {"role": "user", "content": "# Target subagent slice"}}),
        ]) + "\n",
        encoding="utf-8",
    )
    parsed_sessions = read_sessions("omp", [str(omp_root)])
    assert [s["external_id"] for s in parsed_sessions] == ["omp:main-1"]
    assert parsed_sessions[0]["title"] == "重构计费卡片样式"
