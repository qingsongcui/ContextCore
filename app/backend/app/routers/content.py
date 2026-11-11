from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query, Path
from app.models import (
    Content,
    ContentCreate,
    ContentUpdate,
    ContentPinRequest,
    ContentConsolidateRequest,
    ContentRestoreRequest,
    ContentBatchRequest,
    ContentMergeRequest,
)
from app.services import content_service

router = APIRouter(tags=["content"])


@router.get("/api/content", response_model=list[Content])
def list_content_endpoint(
    project_id: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    agent: str | None = None,
    tag: str | None = None,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    pinned: bool | None = None,
    pinned_first: bool = False,
    summary: bool = True,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return content_service.list_content(
        project_id=project_id,
        kind=kind,
        category=category,
        agent=agent,
        tag=tag,
        query=query,
        since=since,
        until=until,
        status=status,
        scope=scope,
        pinned=pinned,
        pinned_first=pinned_first,
        summary=summary,
        limit=limit,
        offset=offset,
    )


@router.get("/api/content/index")
def content_index_endpoint(
    project_id: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    agent: str | None = None,
    tag: str | None = None,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    pinned: bool | None = None,
    pinned_first: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return content_service.content_index(
        project_id=project_id,
        kind=kind,
        category=category,
        agent=agent,
        tag=tag,
        query=query,
        since=since,
        until=until,
        status=status,
        scope=scope,
        pinned=pinned,
        pinned_first=pinned_first,
        limit=limit,
        offset=offset,
    )


@router.get("/api/content/handbook")
def export_memory_handbook_endpoint(
    project_id: str | None = None,
    include_global: bool = True,
):
    return content_service.export_memory_handbook(
        project_id=project_id,
        include_global=include_global,
    )


@router.post("/api/content/consolidate")
def consolidate_memories_endpoint(req: ContentConsolidateRequest):
    return content_service.consolidate_memories(
        project_id=req.project_id,
        scope=req.scope,
    )


@router.post("/api/content", response_model=Content)
def create_content_endpoint(req: ContentCreate):
    return content_service.create_content(
        title=req.title,
        content=req.content,
        project_id=req.project_id,
        kind=req.kind,
        category=req.category,
        tags=req.tags,
        pinned=req.pinned,
        scope=req.scope,
        agent=req.agent,
        source_id=req.source_id,
        source_kind=req.source_kind,
        quote=req.quote,
        related_ids=req.related_ids,
        supersedes_id=req.supersedes_id,
    )


@router.post("/api/content/{content_id}/pin", response_model=Content)
def pin_content_endpoint(content_id: str, req: ContentPinRequest):
    return content_service.pin_content(
        content_id=content_id,
        pinned=req.pinned,
        expected_version=req.expected_version,
    )


@router.get("/api/content/{content_id}/versions")
def list_content_versions_endpoint(
    content_id: str,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return content_service.content_versions_page(content_id, limit=limit, offset=offset)


@router.get("/api/content/{content_id}/versions/{version}")
def get_content_version_endpoint(content_id: str, version: int = Path(..., ge=1)):
    return content_service.get_content_version(content_id, version)


@router.get("/api/content/{content_id}", response_model=Content)
def get_content_endpoint(content_id: str, include_versions: bool = False):
    return content_service.get_content(content_id, include_versions=include_versions)


@router.patch("/api/content/{content_id}", response_model=Content)
def update_content_endpoint(content_id: str, req: ContentUpdate):
    return content_service.update_content(
        content_id=content_id,
        title=req.title,
        content=req.content,
        category=req.category,
        tags=req.tags,
        pinned=req.pinned,
        scope=req.scope,
        agent=req.agent,
        status=req.status,
        project_id=req.project_id,
        expected_version=req.expected_version,
    )


@router.delete("/api/content/{content_id}")
def delete_content_endpoint(content_id: str, expected_version: int = Query(..., ge=1)):
    return content_service.delete_content(content_id, expected_version=expected_version)


@router.post("/api/content/{content_id}/restore", response_model=Content)
def restore_content_version_endpoint(content_id: str, req: ContentRestoreRequest):
    return content_service.restore_version(
        content_id=content_id,
        version=req.version,
        expected_version=req.expected_version,
    )


@router.post("/api/content/batch")
def batch_content_endpoint(req: ContentBatchRequest):
    if req.action == "delete":
        if req.expected_versions is None:
            raise HTTPException(status_code=422, detail="expected_versions is required for batch deletion")
    if req.expected_versions is not None:
        if len(req.ids) != len(set(req.ids)) or set(req.expected_versions) != set(req.ids):
            raise HTTPException(status_code=422, detail="expected_versions must match each unique requested id")
    return content_service.batch_content(
        ids=req.ids,
        action=req.action,
        tags=req.tags,
        category=req.category,
        expected_versions=req.expected_versions,
    )


@router.post("/api/content/merge", response_model=Content)
def merge_content_endpoint(req: ContentMergeRequest):
    if req.expected_versions is None:
        raise HTTPException(status_code=422, detail="expected_versions is required for content merge")
    if len(req.ids) != len(set(req.ids)) or set(req.expected_versions) != set(req.ids):
        raise HTTPException(status_code=422, detail="expected_versions must match each unique source id")
    return content_service.merge_content(
        ids=req.ids,
        title=req.title,
        content=req.content,
        project_id=req.project_id,
        expected_versions=req.expected_versions,
    )


@router.post("/api/import", response_model=Content)
async def import_document_endpoint(
    file: UploadFile = File(...),
    project_id: str | None = Form(None),
):
    if not file.filename:
        raise HTTPException(status_code=422, detail="No filename provided")
    from app.services.bounded_import import import_upload

    return await import_upload(
        upload=file,
        filename=file.filename,
        project_id=project_id,
    )
