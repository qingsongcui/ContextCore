from fastapi import APIRouter, HTTPException
from app.models import SettingsResponse, SettingsPatch
from app.database import db, get_app_setting, set_app_setting, log_audit

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=SettingsResponse)
def get_settings_endpoint():
    try:
        from app.runtime import autostart_status
        status = autostart_status()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Autostart status is unavailable") from exc

    compute_host_val = get_app_setting("compute_host", default=None)
    compute_host = compute_host_val if compute_host_val == "omp" else None
    compute_authorized = bool(get_app_setting("compute_authorized", default=False))

    return SettingsResponse(
        autostart=bool(status.get("autostart", status.get("desired", True))),
        autostart_installed=bool(status.get("autostart_installed", status.get("installed", False))),
        platform=str(status.get("platform", "darwin")),
        compute_host=compute_host,
        compute_authorized=compute_authorized,
    )


@router.patch("", response_model=SettingsResponse)
def update_settings_endpoint(req: SettingsPatch):
    fields_set = req.model_fields_set
    updates = {}
    if "compute_host" in fields_set:
        if req.compute_host not in (None, "omp"):
            raise HTTPException(status_code=400, detail="Only OMP currently supports constrained compute execution")
        if req.compute_host == "omp" and req.compute_confirmed is not True:
            raise HTTPException(status_code=400, detail="Choosing a compute host requires explicit confirmation")
        updates["compute_host"] = req.compute_host
        if req.compute_host is None:
            updates["compute_authorized"] = False
    if "compute_authorized" in fields_set and req.compute_authorized is not None:
        if req.compute_authorized and req.compute_confirmed is not True:
            raise HTTPException(status_code=400, detail="Authorizing compute requires explicit confirmation")
        updates["compute_authorized"] = req.compute_authorized
    requested_host = updates.get("compute_host", get_app_setting("compute_host", None))
    requested_authorization = updates.get("compute_authorized", get_app_setting("compute_authorized", False))
    if requested_authorization and requested_host != "omp":
        raise HTTPException(status_code=400, detail="Select OMP before authorizing compute")
    if req.autostart is not None:
        try:
            from app.runtime import install_autostart, set_autostart_desired
            if req.install_launch_agent and req.autostart:
                installed = install_autostart(register=True)
                if not installed.get("ok"):
                    raise HTTPException(
                        status_code=503,
                        detail=installed.get("error") or "Autostart login item was not registered",
                    )
            outcome = set_autostart_desired(req.autostart)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Autostart preference was not changed") from exc
        if not outcome.get("ok"):
            raise HTTPException(status_code=409, detail="Autostart preference could not be changed safely")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        host = updates.get("compute_host", get_app_setting("compute_host", None, conn=conn))
        authorized = updates.get("compute_authorized", get_app_setting("compute_authorized", False, conn=conn))
        if authorized and host != "omp":
            raise HTTPException(status_code=400, detail="Select OMP before authorizing compute")
        if req.autostart is not None:
            log_audit(actor="user", action="settings.autostart", target="autostart", conn=conn)
        for key, value in updates.items():
            set_app_setting(key, value, conn=conn)
        if updates:
            log_audit(actor="user", action="settings.compute", target="compute", conn=conn)
    return get_settings_endpoint()


@router.post("/backup")
def create_database_backup_endpoint():
    from app.database import backup_database
    try:
        return backup_database()
    except OSError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Database backup failed") from exc

