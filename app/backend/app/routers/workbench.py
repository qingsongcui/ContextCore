from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services import workbench_service as service

router = APIRouter(prefix="/api/workbench", tags=["workbench"])


class Evidence(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)


class CandidateQuery(BaseModel):
    project_ids: list[str] = Field(max_length=200)
    offset: int = Field(default=0, ge=0)


@router.post("/candidates")
def candidates(req: CandidateQuery):
    return service.capability_candidates(req.project_ids, req.offset)


class Asset(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    excerpt: str = Field(default="", max_length=4000)
    notes: str = Field(default="", max_length=8000)
    inputs: str = Field(default="", max_length=2000)
    steps: str = Field(default="", max_length=8000)
    outputs: str = Field(default="", max_length=2000)
    verification: str = Field(default="", max_length=2000)
    source_id: str = Field(default="", max_length=200)
    source_version: int = Field(default=0, ge=0)
    selected: bool = False
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    candidate_topic: str = Field(default="", max_length=160)
    contribution: str = Field(default="", max_length=2000)


class Profile(BaseModel):
    revision: int = Field(ge=0)
    name: str = Field(default="", max_length=160)
    bio: str = Field(default="", max_length=4000)
    hidden_topics: list[str] = Field(default_factory=list, max_length=1000)
    assets: list[Asset] = Field(default_factory=list, max_length=200)


@router.get("/memories")
def memories(project_id: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=200)):
    return service.reading_page(project_id, offset, limit)


@router.get("/profile")
def profile():
    return service.get_profile()


@router.put("/profile")
def save(req: Profile):
    return service.save_profile(req)


@router.get("/profile/export")
def export():
    return service.profile_snapshot()
