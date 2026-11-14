from fastapi import APIRouter
from app.models import Project, ProjectCreate, ProjectUpdate, ProjectMergeRequest, ProjectSplitRequest
from app.services import project_service

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[Project])
def list_projects_endpoint(status: str | None = None):
    return project_service.list_projects(status=status)


@router.post("", response_model=Project)
def create_project_endpoint(req: ProjectCreate):
    return project_service.create_project(
        name=req.name,
        description=req.description,
        directories=req.directories,
        manual=True,
    )


@router.patch("/{project_id}", response_model=Project)
def update_project_endpoint(project_id: str, req: ProjectUpdate):
    return project_service.update_project(
        project_id=project_id,
        name=req.name,
        description=req.description,
        directories=req.directories,
        status=req.status,
    )


@router.post("/merge", response_model=Project)
def merge_projects_endpoint(req: ProjectMergeRequest):
    return project_service.merge_projects(
        source_ids=req.source_ids,
        target_id=req.target_id,
    )


@router.post("/{project_id}/split", response_model=Project)
def split_project_endpoint(project_id: str, req: ProjectSplitRequest):
    return project_service.split_project(
        project_id=project_id,
        name=req.name,
        description=req.description,
        conversation_ids=req.conversation_ids,
        content_ids=req.content_ids,
        directories=req.directories,
    )
