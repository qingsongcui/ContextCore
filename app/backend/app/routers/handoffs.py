from typing import Any, Literal
from fastapi import APIRouter, Body, Query, HTTPException
from app.models import Handoff, HandoffCreateRequest
from app.services import handoff_service

router = APIRouter(prefix="/api/handoffs", tags=["handoffs"])


@router.get("", response_model=list[Handoff])
def list_handoffs_endpoint(project_id: str | None = None, language: Literal['zh', 'en'] = 'zh'):
    return handoff_service.list_handoffs(project_id=project_id, language=language)


@router.post("", response_model=Handoff)
def create_handoff_endpoint(req: HandoffCreateRequest):
    return handoff_service.create_handoff(
        project_id=req.project_id,
        target_agent=req.target_agent,
        task_id=req.task_id,
        conversation_id=req.conversation_id,
        query=req.query,
        source_agent=req.source_agent,
        actor="user",
        language=req.language,
    )


@router.get("/relay/candidates")
def list_relay_candidates_endpoint(
    consumer: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    sync_first: bool = Query(default=True),
    language: Literal['zh', 'en'] = 'zh',
):
    return handoff_service.discover_relay_projects(
        consumer=consumer,
        limit=limit,
        sync_first=sync_first,
        language=language,
    )


@router.get("/relay/conversations")
def list_relay_conversations_endpoint(
    consumer: str | None = Query(default=None),
    limit: int = Query(default=15, ge=1, le=50),
    sync_first: bool = Query(default=True),
    language: Literal['zh', 'en'] = 'zh',
    cwd: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
):
    return handoff_service.discover_relay_conversations(
        consumer=consumer,
        limit=limit,
        sync_first=sync_first,
        language=language,
        cwd=cwd,
        project_id=project_id,
    )


@router.get("/relay/conversation")
def relay_conversation_get_endpoint(
    conversation_id: str | None = Query(default=None),
    selection: str | None = Query(default=None),
    consumer: str | None = Query(default=None),
    target_agent: str | None = Query(default=None),
    max_tokens: int = Query(default=12000, ge=64, le=32000),
    sync_first: bool = Query(default=True),
    language: Literal['zh', 'en'] = 'zh',
    query: str | None = Query(default=None),
    cwd: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
):
    return handoff_service.relay_conversation(
        conversation_id=conversation_id or selection,
        consumer=consumer or target_agent,
        max_tokens=max_tokens,
        sync_first=sync_first,
        language=language,
        query=query,
        cwd=cwd,
        project_id=project_id,
    )


@router.post("/relay/conversation")
def relay_conversation_endpoint(payload: dict[str, Any] = Body(default={})):
    return handoff_service.relay_conversation(
        conversation_id=payload.get("conversation_id") or payload.get("selection"),
        consumer=payload.get("consumer") or payload.get("target_agent"),
        max_tokens=payload.get("max_tokens", 12000),
        sync_first=payload.get("sync_first", True),
        language=payload.get("language", "zh"),
        query=payload.get("query"),
        cwd=payload.get("cwd"),
        project_id=payload.get("project_id"),
    )


@router.post("/relay")
def relay_project_endpoint(payload: dict[str, Any] = Body(default={})):
    if "auto_confirm" in payload:
        raise HTTPException(status_code=422, detail="Relay cannot confirm a handoff; use target-Agent confirmation after user readback.")
    return handoff_service.relay_project(
        project_id=payload.get("project_id"),
        consumer=payload.get("consumer") or payload.get("target_agent"),
        context_mode=payload.get("context_mode", "compressed"),
        task_id=payload.get("task_id"),
        conversation_id=payload.get("conversation_id"),
        query=payload.get("query"),
        max_tokens=payload.get("max_tokens", 4000),
        sync_first=payload.get("sync_first", True),
        language=payload.get('language', 'zh'),
        handoff_id=payload.get('handoff_id'),
    )


@router.get("/{handoff_id}", response_model=Handoff)
def get_handoff_endpoint(handoff_id: str, language: Literal['zh', 'en'] = 'zh'):
    return handoff_service.get_handoff_for_rest(handoff_id, language)


@router.post("/{handoff_id}/cancel", response_model=Handoff)
def cancel_handoff_endpoint(handoff_id: str):
    return handoff_service.cancel_handoff(handoff_id=handoff_id)
