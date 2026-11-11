from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.services import retrieval_service, semantic_service

router = APIRouter(prefix="/api/retrieval", tags=["retrieval"])


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchRequest(StrictRequest):
    query: str = Field(default="", max_length=8000)
    project_id: str | None = None
    limit: int = Field(default=15, ge=1, le=100)
    mode: Literal["lexical", "hybrid", "semantic"] = "lexical"


class ProtectedEntry(StrictRequest):
    id: str
    text: str = Field(max_length=100000)


class AssembleRequest(StrictRequest):
    query: str = Field(default="", max_length=8000)
    project_id: str | None = None
    budget_tokens: int = Field(default=2000, ge=1, le=32000)
    context_epoch: str = Field(default="default", min_length=1, max_length=200)
    consumer_id: str | None = Field(default=None, min_length=1, max_length=200)
    protected: list[ProtectedEntry] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=15, ge=1, le=100)


class SettingsPatch(StrictRequest):
    lexical_enabled: bool | None = None
    semantic_enabled: bool | None = None
    relation_enabled: bool | None = None
    max_results: int | None = Field(default=None, ge=1, le=100)
    context_budget_tokens: int | None = Field(default=None, ge=64, le=32000)


class RelationRequest(StrictRequest):
    from_id: str
    to_id: str
    relation: str = Field(min_length=1, max_length=100)
    evidence_id: str
    evidence_quote: str = Field(min_length=1, max_length=4000)
    semantic: bool = False


class PackVerifyRequest(StrictRequest):
    pack_path: str = Field(min_length=1, max_length=4000)


class RelationExtractRequest(StrictRequest):
    content_ids: list[str] = Field(min_length=1, max_length=20)
    retry: bool = False


class DownloadRequest(StrictRequest):
    confirmed: bool = False
    download_id: UUID | None = None


class DownloadCancelRequest(StrictRequest):
    download_id: UUID


class RuntimePrepareRequest(StrictRequest):
    confirmed: bool = False
    plan_id: str = Field(min_length=1, max_length=128)


@router.get("/status")
def status_endpoint():
    return retrieval_service.get_status()


@router.get("/settings")
def settings_endpoint():
    return retrieval_service.get_settings()


@router.patch("/settings")
def settings_patch_endpoint(request: SettingsPatch):
    return retrieval_service.update_settings(request.model_dump(exclude_none=True))


@router.post("/search")
def search_endpoint(request: SearchRequest):
    return retrieval_service.search(**request.model_dump())


@router.post("/assemble")
def assemble_endpoint(request: AssembleRequest):
    from app.security import authenticated_agent
    payload = request.model_dump()
    payload["consumer_id"] = authenticated_agent.get() or "web"
    return retrieval_service.bound_context_response(
        retrieval_service.assemble_context(**payload), request.budget_tokens,
    )


@router.get("/evidence/{evidence_id}")
def evidence_endpoint(evidence_id: str, offset: int = Query(0, ge=0), limit: int = Query(2000, ge=1, le=8000)):
    return retrieval_service.read_evidence(evidence_id, offset, limit)


@router.post("/packs/{pack_id}/ack")
def pack_ack_endpoint(pack_id: str):
    return retrieval_service.record_injection(pack_id)


@router.get("/relations")
def relations_endpoint(entity_id: str | None = None):
    return retrieval_service.list_relations(entity_id)


@router.post("/relations")
def relation_endpoint(request: RelationRequest):
    return retrieval_service.create_relation(**request.model_dump())


@router.post("/relations/extract")
async def relation_extract_endpoint(request: RelationExtractRequest):
    return await retrieval_service.extract_relations(request.content_ids, retry=request.retry)


@router.get("/semantic/manifest")
def semantic_manifest_endpoint():
    return semantic_service.manifest()


@router.get("/semantic/runtime/plan")
def semantic_runtime_plan_endpoint():
    return semantic_service.runtime_plan()


@router.post("/semantic/runtime/prepare")
def semantic_runtime_prepare_endpoint(request: RuntimePrepareRequest):
    return semantic_service.prepare_runtime(**request.model_dump())


@router.post("/semantic/verify")
def semantic_verify_endpoint(request: PackVerifyRequest):
    return semantic_service.verify_local_pack(request.pack_path)


@router.post("/semantic/download")
def semantic_download_endpoint(request: DownloadRequest):
    return semantic_service.download_model_job(confirmed=request.confirmed,
                                              download_id=str(request.download_id) if request.download_id else None)


@router.post("/semantic/download/cancel")
def semantic_download_cancel_endpoint(request: DownloadCancelRequest):
    return semantic_service.cancel_download(str(request.download_id))


@router.post("/semantic/index/{entity_id}")
def semantic_index_endpoint(entity_id: str):
    return semantic_service.index_content(entity_id)
