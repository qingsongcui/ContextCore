"""End-to-end local semantics using only synthetic conversations."""
import pytest
from fastapi import HTTPException
from app.database import create_schema, seed_data, db
from app.routers.workbench import Profile
from app.services import content_service, conversation_service, distillation_service, project_service
from app.services import retrieval_service, workbench_service


@pytest.fixture
def projects():
    create_schema()
    seed_data()
    return [project_service.create_project(name=n)["id"] for n in ("Weekly", "Client", "Other")]


def capture(project, claim):
    conv = conversation_service.upsert_conversation(agent="codex", project_id=project, title=claim,
        messages=[{"role":"user", "content":claim}])
    distillation_service.auto_summarize_memory(conversation_id=conv["id"], actor="codex")
    return conv["id"]


def constraints(project):
    return [r["content"] for r in retrieval_service.bootstrap_working_memory(project_id=project)["applicable_constraints"]]


def test_change_rule_and_replay(projects):
    project = projects[0]
    early = capture(project, "最终决定周报按自然周统计")
    capture(project, "最终决定周报改用财务周统计")
    distillation_service.auto_summarize_memory(conversation_id=early, actor="codex")
    current = workbench_service.reading_page(project, 0, 40)["items"]
    assert len(current) == 1
    assert "财务周" in current[0]["heading"]
    assert "自然周" in current[0]["previous"][0]["title"]
    assert constraints(project) == ["最终决定周报改用财务周统计"]
    handbook = content_service.export_memory_handbook(project)
    assert "自然周" not in str(handbook)
    assert "财务周" in str(handbook)


def test_personal_default_and_project_exception(projects):
    first, client, other = projects
    capture(first, "我的所有项目以后都用中文写结论")
    capture(client, "本项目所有报告默认使用英文")
    capture(other, "这次只用中文输出")
    assert len(workbench_service.reading_page(None, 0, 40)["items"]) == 1
    assert constraints(other) == ["我的所有项目以后都用中文写结论"]
    assert constraints(client) == ["本项目所有报告默认使用英文"]
    assert "中文" not in str(content_service.export_memory_handbook(client))
    assert "中文" not in str(workbench_service.reading_page(client, 0, 40))


def test_profile_versions_and_selection(projects):
    capture(projects[0], "最终决定周报按自然周统计")
    evidence = workbench_service.reading_page(projects[0], 0, 10)["items"][0]
    form = Profile(revision=0, name="Research", assets=[
        {"id":"visible","title":"Report QA","excerpt":"clean sample","notes":"private note","selected":True,
         "source_id":evidence["id"], "source_version":evidence["version"]},
        {"id":"hidden","title":"Private project"},
    ])
    assert workbench_service.save_profile(form)["revision"] == 1
    with pytest.raises(HTTPException) as rejected:
        workbench_service.save_profile(form)
    assert rejected.value.status_code == 409
    assert workbench_service.profile_snapshot()["assets"] == [{"title":"Report QA","excerpt":"clean sample"}]
    content_service.delete_content(evidence["id"],expected_version=evidence["version"])
    assert workbench_service.get_profile()["assets"][0]["source_title"] is None


def test_beta4_upgrade_repairs_supported_history_without_changing_ids(projects):
    from app.database import set_app_setting
    from app.services import memory_rules
    p = projects[0]
    capture(p, "最终决定周报按自然周统计")
    capture(p, "最终决定周报改用财务周统计")
    capture(p, "我的所有项目以后都用中文写结论")
    with db() as conn:
        # Recreate the Beta.4 layout while preserving authentic source versions and authorship.
        conn.execute("DELETE FROM content_versions WHERE version>1")
        conn.execute("UPDATE contents SET version=1,status='active', superseded_by_id=NULL, related_ids='[]'")
        conn.execute("UPDATE contents SET project_id=? WHERE category='preference'", (p,))
        original = [r[0] for r in conn.execute("SELECT id FROM contents ORDER BY id")]
        set_app_setting("personal_workbench_memory_migration_v1",False,conn=conn)
        memory_rules.migrate_beta4_rules(conn)
        memory_rules.migrate_beta4_rules(conn)
        assert original == [r[0] for r in conn.execute("SELECT id FROM contents ORDER BY id")]
    assert len(workbench_service.reading_page(None,0,20)["items"]) == 1
    decisions = [r for r in workbench_service.reading_page(p,0,20)["items"] if r["category"] == "decision"]
    assert len(decisions) == 1 and len(decisions[0]["previous"]) == 1


def test_capability_candidates_use_only_selected_live_evidence(projects):
    for project in projects[:2]:
        content_service.create_content(title="核对周报指标", content="example", kind="document", project_id=project)
    content_service.create_content(title="private example", content="other",kind="document", project_id=projects[2])
    result = workbench_service.capability_candidates(projects[:2],0)
    assert len(result["items"]) == 1
    assert result["items"][0]["projects"] == 2
    assert {r["project_id"] for r in result["items"][0]["evidence"]} == set(projects[:2])
    assert workbench_service.capability_candidates([],0)["items"] == []


def test_reader_is_consistent_with_detail_and_edited_defaults(projects):
    capture(projects[0], "最终决定周报按自然周统计")
    capture(projects[0], "最终决定周报改用财务周统计")
    item = workbench_service.reading_page(projects[0], 0, 1)["items"][0]
    assert content_service.get_content(item["id"])["heading"] == item["heading"] == "周报按财务周统计"
    capture(projects[0], "我的所有项目以后都用中文写结论")
    capture(projects[1], "我的所有项目以后改用英文写结论")
    assert constraints(projects[2]) == ["我的所有项目以后改用英文写结论"]


def test_explicit_rule_survives_ambiguous_and_out_of_order_updates(projects):
    p = projects[0]
    def make(claim):
        return conversation_service.upsert_conversation(agent="codex", project_id=p, title=claim,
            messages=[{"role":"user","content":claim}])["id"]
    older = make("最终决定周报改用自然周统计")
    capture(p, "最终决定周报按财务周统计")
    distillation_service.auto_summarize_memory(conversation_id=older,actor="codex")
    assert constraints(p) == ["最终决定周报按财务周统计"]
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM conflicts WHERE project_id=? AND status='pending'",(p,)).fetchone()[0] == 1


def test_discussion_stays_searchable_outside_current_memory(projects):
    capture(projects[0], "如果最终决定周报改用自然周统计，需要和客户再商量")
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations WHERE project_id=?", (projects[0],)).fetchone()[0] == 1
    assert not workbench_service.reading_page(projects[0], 0, 20)["items"]
    assert not retrieval_service.bootstrap_working_memory(project_id=projects[0])["relevant_lessons"]
