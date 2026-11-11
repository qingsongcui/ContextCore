from typing import Any
from fastapi import APIRouter, Body, Query

router = APIRouter(prefix="/api/processing", tags=["processing"])


@router.get("/jobs")
def list_jobs(status: str | None = None, limit: int = Query(100, ge=1, le=500)):
    from app.services.processing_jobs import list_processing_jobs
    return {"items": list_processing_jobs(status=status, limit=limit),
            "coverage_note": "Submission, processing and host readback are separate states."}


@router.get("/cleanup")
def cleanup_jobs(limit: int = Query(100, ge=1, le=500)):
    from app.services.derivation_service import list_cleanup_jobs
    items = list_cleanup_jobs(limit)
    return {"items": items, "pending": sum(j["state"] != "completed" for j in items)}


@router.post("/auto-summarize")
def auto_summarize_endpoint(payload: dict[str, Any] = Body(default={})):
    from app.services.distillation_service import auto_summarize_memory
    return auto_summarize_memory(
        project_id=payload.get("project_id"),
        conversation_id=payload.get("conversation_id"),
        session_summary=payload.get("session_summary"),
        memories=payload.get("memories"),
        messages=payload.get("messages"),
        cwd=payload.get("cwd"),
        actor=str(payload.get("actor") or "host_agent"),
        auto_extract_from_messages=bool(payload.get("auto_extract_from_messages", True)),
    )
