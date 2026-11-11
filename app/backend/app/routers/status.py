import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from app.config import settings
from app.database import db
from app.models import StatusResponse, ServiceStatus, CountsStatus, ProcessingStatus, UsageStatus, ServiceStopRequest

router = APIRouter(tags=["status"])


def _get_runtime_service_summary() -> dict[str, Any]:
    try:
        from app.runtime import get_service_status_summary
        return get_service_status_summary()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Service lifecycle status is unavailable") from exc


@router.get("/health")
def health_check():
    return {"ok": True, "version": settings.app_version}


@router.get("/api/release")
def release_status(request: Request):
    return getattr(request.app.state, "release", {
        "release_id": "development", "api_contract": 2, "frontend_sha256": None,
    })


@router.get("/api/status", response_model=StatusResponse)
def get_system_status():
    svc = _get_runtime_service_summary()
    with db() as conn:
        p_count = conn.execute("SELECT COUNT(*) as c FROM projects WHERE status = 'active'").fetchone()["c"]
        c_count = conn.execute("SELECT COUNT(*) as c FROM conversations").fetchone()["c"]
        m_count = conn.execute("SELECT COUNT(*) as c FROM contents WHERE kind = 'memory' AND status = 'active'").fetchone()["c"]
        d_count = conn.execute("SELECT COUNT(*) as c FROM contents WHERE kind = 'document' AND status = 'active'").fetchone()["c"]
        cf_count = conn.execute("SELECT COUNT(*) as c FROM conflicts WHERE status = 'pending'").fetchone()["c"]

        proc_pending = conn.execute("SELECT COUNT(*) as c FROM conversations WHERE processing_status = 'pending'").fetchone()["c"]
        proc_errors = conn.execute("SELECT COUNT(*) as c FROM conversations WHERE processing_status = 'error'").fetchone()["c"]
        usage = conn.execute(
            """SELECT COUNT(*) AS records, COALESCE(SUM(requests), 0) AS requests,
                      COALESCE(SUM(tokens), 0) AS tokens,
                      COALESCE(SUM(CASE WHEN tokens IS NULL THEN 1 ELSE 0 END), 0) AS unmetered
               FROM usage_meters"""
        ).fetchone()

    return StatusResponse(
        ok=True,
        version=settings.app_version,
        service=ServiceStatus(
            running=bool(svc.get("running", False)),
            autostart=bool(svc.get("autostart", True)),
            autostart_installed=bool(svc.get("autostart_installed", False)),
            platform=str(svc.get("platform", "darwin")),
            stop_status=svc.get("stop_status"),
            stop_error=svc.get("stop_error"),
        ),
        counts=CountsStatus(
            projects=p_count,
            conversations=c_count,
            memories=m_count,
            documents=d_count,
            pending_conflicts=cf_count,
        ),
        processing=ProcessingStatus(
            pending=proc_pending,
            errors=proc_errors,
        ),
        usage=UsageStatus(
            tokens=None if usage["unmetered"] else usage["tokens"],
            requests=usage["requests"],
            note=(
                f"已记录 {usage['requests']} 次提炼请求；其中 {usage['unmetered']} 条未返回 Token 用量，总 Token 未知。"
                if usage["unmetered"] else
                "仅统计 ContextCore 发起且已记录的提炼请求，不代表宿主账号全部用量。"
            ),
        ),
    )


@router.post("/api/service/stop")
def stop_service_endpoint(req: ServiceStopRequest, background_tasks: BackgroundTasks):
    if not req.confirmed:
        raise HTTPException(status_code=422, detail="Stopping service requires confirmed=True")
    from app.runtime import is_service_running, resolve_data_dir, write_shutdown_receipt

    data_dir = resolve_data_dir()
    running, pid = is_service_running(data_dir)
    if not running or pid != os.getpid():
        raise HTTPException(status_code=503, detail="Cannot verify the identity of this service process")

    # Record narrow metadata-only receipt showing stop is pending
    write_shutdown_receipt(
        data_dir=data_dir,
        status="pending",
        pid=pid,
        stopped=False,
        ok=True,
        message="Service stop scheduled; shutdown in progress",
    )

    # An external controller waits for this process to exit. Calling the
    # synchronous stop routine inside its own HTTP request deadlocks shutdown.
    def launch_stop_controller():
        try:
            log_dir = data_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "stop.log"
            stop_log = open(log_path, "a", encoding="utf-8")
            subprocess.Popen(
                [sys.executable, "-m", "app.cli", "stop", "--data-dir", str(data_dir)],
                cwd=str(Path(__file__).resolve().parents[2]),
                stdin=subprocess.DEVNULL,
                stdout=stop_log,
                stderr=stop_log,
                start_new_session=True,
            )
        except Exception as exc:
            write_shutdown_receipt(
                data_dir=data_dir,
                status="failed",
                pid=pid,
                ok=False,
                error=f"Failed to launch stop controller: {exc}",
                stopped=False,
            )

    background_tasks.add_task(launch_stop_controller)
    return {"ok": True, "status": "stopping", "message": "Service stop scheduled; shutdown is not yet confirmed"}
