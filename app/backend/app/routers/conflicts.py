from fastapi import APIRouter
from app.models import Conflict, ConflictResolveRequest, ProposedContent
from app.services import conflict_service
from pydantic import BaseModel

router = APIRouter(prefix="/api/conflicts", tags=["conflicts"])


class ConflictCreateRequest(BaseModel):
    project_id: str | None = None
    existing_id: str
    proposed: ProposedContent
    reason: str


@router.get("", response_model=list[Conflict])
def list_conflicts_endpoint(project_id: str | None = None, status: str | None = None):
    return conflict_service.list_conflicts(project_id=project_id, status=status)


@router.post("", response_model=Conflict)
def create_conflict_endpoint(req: ConflictCreateRequest):
    return conflict_service.create_conflict(
        project_id=req.project_id,
        existing_id=req.existing_id,
        proposed=req.proposed.model_dump(),
        reason=req.reason,
    )


@router.get("/{conflict_id}", response_model=Conflict)
def get_conflict_endpoint(conflict_id: str):
    return conflict_service.get_conflict(conflict_id)


@router.post("/{conflict_id}/resolve", response_model=Conflict)
def resolve_conflict_endpoint(conflict_id: str, req: ConflictResolveRequest):
    return conflict_service.resolve_conflict(
        conflict_id=conflict_id,
        resolution=req.resolution,
        expected_version=req.expected_version,
        merged_title=req.merged_title,
        merged_content=req.merged_content,
        merged_category=req.merged_category,
    )
