from fastapi import APIRouter, Query
from app.models import Conversation, ConversationCreate, ConversationUpdate
from app.services import conversation_service

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("", response_model=list[Conversation])
def list_conversations_endpoint(
    project_id: str | None = None,
    query: str | None = None,
    agent: str | None = None,
    since: str | None = None,
    until: str | None = None,
    summary: bool = True,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return conversation_service.list_conversations(
        project_id=project_id,
        query=query,
        agent=agent,
        since=since,
        until=until,
        summary=summary,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=Conversation)
def create_conversation_endpoint(req: ConversationCreate):
    return conversation_service.create_conversation(
        agent=req.agent,
        title=req.title,
        project_id=req.project_id,
        source_path=req.source_path,
        messages=[m.model_dump() for m in req.messages],
        process_summary=req.process_summary,
    )


@router.get("/index")
def conversation_index_endpoint(
    project_id: str | None = None,
    query: str | None = None,
    agent: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return conversation_service.conversation_index(
        project_id=project_id, query=query, agent=agent, since=since,
        until=until, limit=limit, offset=offset,
    )


@router.get("/{conversation_id}/messages")
def conversation_messages_endpoint(
    conversation_id: str,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return conversation_service.conversation_messages_page(conversation_id, limit=limit, offset=offset)


@router.get("/{conversation_id}", response_model=Conversation)
def get_conversation_endpoint(conversation_id: str, include_messages: bool = False):
    return conversation_service.get_conversation(conversation_id, include_messages=include_messages)


@router.patch("/{conversation_id}", response_model=Conversation)
def update_conversation_endpoint(conversation_id: str, req: ConversationUpdate):
    return conversation_service.update_conversation(
        conversation_id=conversation_id,
        title=req.title,
        project_id=req.project_id,
    )


@router.delete("/{conversation_id}")
def delete_conversation_endpoint(conversation_id: str):
    return conversation_service.delete_conversation(conversation_id)
