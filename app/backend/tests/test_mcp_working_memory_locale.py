"""Exercise the Agent-facing language contract without provider credentials."""
import pytest

from app import mcp_server
from app.database import create_schema
from app.security import authenticated_agent
from app.services import content_service, project_service, retrieval_service


@pytest.fixture
def agent_project():
    create_schema()
    project = project_service.create_project(name="Orchard QA")
    token = authenticated_agent.set("opencode")
    try:
        yield project["id"]
    finally:
        authenticated_agent.reset(token)


def test_default_english_memory_receipt_and_explicit_chinese_receipt(agent_project):
    saved = mcp_server.save_memory(
        project_id=agent_project, title="Weekly format",
        content="Always use the orchard report template.", category="preference",
    )
    assert saved["summary_note"] == "Memory saved: Weekly format"

    outdated = mcp_server.mark_memory_outdated(saved["id"], language="zh")
    assert outdated["summary_note"] == "已将记忆标记为过时：Weekly format"


@pytest.mark.parametrize("fallback", [False, True])
def test_bootstrap_language_localizes_metadata_and_preserves_source(agent_project, monkeypatch, fallback):
    original = "Use the fixture named 南区 for this scope."
    content_service.create_content(
        project_id=agent_project, title="Fixture rule", content=original,
        category="preference", applicability={"version": "3.0"}, agent="user",
    )
    if fallback:
        monkeypatch.setattr(retrieval_service, "bootstrap_working_memory", None)
    source = mcp_server.bootstrap_working_memory(project_id=agent_project)
    translated = mcp_server.bootstrap_working_memory(project_id=agent_project, language="zh")
    assert source["applicable_constraints"][0]["content"] == original
    assert translated["applicable_constraints"][0]["content"] == original
    assert "Verify" in source["applicable_constraints"][0]["applicability_warning"]
    assert "适用" in translated["applicable_constraints"][0]["applicability_warning"]
    assert "loaded" in source["feedback_summary"].lower()
    assert "已加载" in translated["feedback_summary"]


def test_discussion_task_default_state_uses_requested_language(agent_project):
    task = mcp_server.upsert_working_task(
        project_id=agent_project, title="Design orchard ingestion",
        execution_mode="discussion_only",
    )
    assert task["must_not_execute"] is True
    assert task["task"]["current_state"] == "Discussion in progress. Code changes await authorization."
