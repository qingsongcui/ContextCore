from fastapi import APIRouter, Body, Query
from app.models import (
    WorkingTask,
    WorkingTaskUpsert,
    WorkingTaskPatch,
    WorkingTaskPromoteRequest,
)
from app.services import working_memory_service

router = APIRouter(tags=["working-tasks"])


@router.get("/api/working-tasks", response_model=list[WorkingTask])
def list_working_tasks_endpoint(
    project_id: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    status: str | None = Query(default=None),
    git_branch: str | None = Query(default=None),
    cwd: str | None = Query(default=None),
    include_events: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return working_memory_service.list_working_tasks(
        project_id=project_id,
        active_only=active_only,
        status=status,
        git_branch=git_branch,
        cwd=cwd,
        include_events=include_events,
        limit=limit,
        offset=offset,
    )


@router.get("/api/working-tasks/summary")
def summarize_working_tasks_endpoint(
    project_id: str | None = Query(default=None),
    git_branch: str | None = Query(default=None),
    cwd: str | None = Query(default=None),
):
    """Server-side totals so clients never infer counts from a truncated page."""
    return working_memory_service.summarize_working_tasks(
        project_id=project_id,
        cwd=cwd,
        git_branch=git_branch,
    )


@router.post("/api/working-tasks", response_model=WorkingTask)
def upsert_working_task_endpoint(req: WorkingTaskUpsert):
    return working_memory_service.upsert_working_task(
        title=req.title,
        project_id=req.project_id,
        task_id=req.task_id,
        cwd=req.cwd,
        git_branch=req.git_branch,
        status=req.status,
        execution_mode=req.execution_mode,
        goal=req.goal,
        completed_steps=req.completed_steps,
        add_completed_step=req.add_completed_step,
        current_state=req.current_state,
        temp_constraints=req.temp_constraints,
        add_temp_constraint=req.add_temp_constraint,
        failed_attempts=req.failed_attempts,
        add_failed_attempt=req.add_failed_attempt,
        blockers=req.blockers,
        add_blocker=req.add_blocker,
        next_steps=req.next_steps,
        agent=req.agent,
        last_agent=req.last_agent,
        source_conversation_id=req.source_conversation_id,
        cited_memory_ids=req.cited_memory_ids,
        citation_note=req.citation_note,
        continue_branch_task=req.continue_branch_task,
        expected_version=req.expected_version,
    )

@router.get("/api/working-tasks/{task_id}/position")
def locate_working_task_endpoint(
    task_id: str, project_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    return working_memory_service.locate_working_task_page(task_id, project_id=project_id, limit=limit)



@router.get("/api/working-tasks/{task_id}", response_model=WorkingTask)
def get_working_task_endpoint(
    task_id: str,
    include_events: bool = Query(default=True),
):
    return working_memory_service.get_working_task(
        task_id=task_id,
        include_events=include_events,
    )


@router.patch("/api/working-tasks/{task_id}", response_model=WorkingTask)
def patch_working_task_endpoint(task_id: str, req: WorkingTaskPatch):
    return working_memory_service.patch_working_task(
        task_id=task_id,
        title=req.title,
        status=req.status,
        execution_mode=req.execution_mode,
        goal=req.goal,
        completed_steps=req.completed_steps,
        add_completed_step=req.add_completed_step,
        current_state=req.current_state,
        temp_constraints=req.temp_constraints,
        add_temp_constraint=req.add_temp_constraint,
        failed_attempts=req.failed_attempts,
        add_failed_attempt=req.add_failed_attempt,
        blockers=req.blockers,
        add_blocker=req.add_blocker,
        next_steps=req.next_steps,
        cwd=req.cwd,
        git_branch=req.git_branch,
        agent=req.agent,
        last_agent=req.last_agent,
        source_conversation_id=req.source_conversation_id,
        cited_memory_ids=req.cited_memory_ids,
        citation_note=req.citation_note,
        summary=req.summary,
        expected_version=req.expected_version,
    )


@router.post("/api/working-tasks/{task_id}/promote")
def promote_working_task_endpoint(task_id: str, req: WorkingTaskPromoteRequest = Body(...)):
    actor = "user" if (not req.actor or req.actor in ("user", "manual", "web")) else req.actor
    return working_memory_service.promote_task_conclusion(
        task_id=task_id,
        title=req.title,
        content=req.content,
        category=req.category,
        applicability=req.applicability,
        tags=req.tags,
        pinned=req.pinned,
        evidence_quote=req.evidence_quote,
        actor=actor,
        complete_task=req.complete_task,
    )


@router.delete("/api/working-tasks/{task_id}")
def delete_working_task_endpoint(
    task_id: str,
    expected_version: int | None = Query(default=None, ge=1),
):
    return working_memory_service.delete_working_task(
        task_id=task_id,
        actor="user",
        expected_version=expected_version,
    )
