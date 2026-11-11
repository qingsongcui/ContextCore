"""Meter the emitted response, not an internal fragment or provider usage."""
import json

import pytest
from fastapi import HTTPException

from app import mcp_server
from app.database import create_schema
from app.security import authenticated_agent
from app.services import content_service, handoff_service, project_service, retrieval_service


def test_exact_serialized_budget_and_one_byte_short():
    payload = {"status": "ready", "text": "中文 tool result 😀" * 5}
    first = retrieval_service.bound_context_response(payload, 32000)
    budget = first["estimated_tokens"]
    # Budget's own decimal width is part of the serialized response.
    for _ in range(5):
        response = retrieval_service.bound_context_response(payload, budget)
        if response["status"] == "insufficient_budget":
            budget = response["required_tokens"]
        else:
            budget = response["estimated_tokens"]
    response = retrieval_service.bound_context_response(payload, budget)
    assert response["estimated_tokens"] == len(json.dumps(response, ensure_ascii=False).encode()) == budget
    refused = retrieval_service.bound_context_response(payload, budget - 1)
    assert refused["status"] == "insufficient_budget"
    assert refused["must_not_execute"] is True
    assert "text" not in refused
    assert refused["continuation"]["resume_from"] is None


@pytest.mark.parametrize("mode", ["full", "compressed"])
def test_long_document_cannot_bypass_budget(mode):
    create_schema()
    project = project_service.create_project(name="Bounded relay")
    task = content_service.create_content(title="Synthetic task", content="Continue this task", category="task",
                                          project_id=project["id"], agent="codex")
    content_service.create_content(title="Large constraint", content="原文不可被静默裁剪" * 8000,
                                    category="decision", project_id=project["id"], agent="codex")
    for budget in (256, 32000):
        result = handoff_service.relay_project(project_id=project["id"], consumer="omp", task_id=task["id"],
                                              context_mode=mode, max_tokens=budget, sync_first=False)
        assert result["status"] == "insufficient_budget"
        assert result["must_not_execute"] is True
        assert not result.get("context_text")
        assert result["continuation"]["resume_from"] is None
    token = authenticated_agent.set("omp")
    try:
        result = mcp_server.assemble_context("continue", project_id=project["id"], max_tokens=256, context_mode=mode)
        assert result["status"] == "insufficient_budget"
        assert result["continuation"]["must_not_execute"] is True
    finally:
        authenticated_agent.reset(token)


@pytest.mark.parametrize("budget", [0, -1, True, "4000", 32001])
def test_relay_rejects_invalid_budget_instead_of_clamping(budget):
    with pytest.raises(HTTPException) as exc:
        handoff_service.relay_project(consumer="omp", max_tokens=budget, sync_first=False)
    assert exc.value.status_code == 422


def _handoff_count() -> int:
    from app.database import db
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0]


def _not_executable(payload: dict) -> None:
    continuation = payload.get("continuation") or {}
    assert payload.get("status") != "ready"
    assert payload.get("must_not_execute") is not False
    assert continuation.get("status") != "ready"
    assert continuation.get("must_not_execute") is not False
    assert not payload.get("handoff_id")
    assert not continuation.get("handoff_id")
    assert not continuation.get("resume_from")


@pytest.mark.parametrize("mode", ["full", "compressed"])
def test_missing_card_never_synthesizes_executable_ready(mode):
    create_schema()
    project = project_service.create_project(name="No predecessor")
    before = _handoff_count()
    refused = handoff_service.relay_project(
        project_id=project["id"], consumer="omp", context_mode=mode,
        query="接着做", max_tokens=32000, sync_first=False,
    )
    _not_executable(refused)
    assert _handoff_count() == before
    token = authenticated_agent.set("omp")
    try:
        assembled = mcp_server.assemble_context(
            "接着做", project_id=project["id"], max_tokens=32000, context_mode=mode,
        )
    finally:
        authenticated_agent.reset(token)
    _not_executable(assembled)
    assert _handoff_count() == before
