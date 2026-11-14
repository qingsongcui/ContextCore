import hashlib
from typing import Any
from fastapi import APIRouter, HTTPException, Request, Body
from app.models import Connection, ConnectionPatch
from app.services import connection_service, conversation_service, project_service
from app.database import now_iso, db, encode

router = APIRouter(prefix="/api/connections", tags=["connections"])


def connection_plan_call(method, connection_id, *args):
    from app.services.connection_plans import ConnectionPlans, public_plan
    from app.integrations.config_transactions import ConfigurationError
    try:
        return public_plan(getattr(ConnectionPlans(), method)(connection_id, *args))
    except ConfigurationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except (ValueError, TypeError, KeyError, OSError):
        raise HTTPException(status_code=409, detail="Connection configuration needs review; no successful apply is confirmed") from None


@router.post("/{connection_id}/plans")
def create_connection_plan(connection_id: str, request: Request, payload: dict[str, Any] = Body(...)):
    return connection_plan_call('create', connection_id, payload, str(request.base_url).rstrip('/'))


@router.get("/{connection_id}/plans/{plan_id}")
def read_connection_plan(connection_id: str, plan_id: str):
    return connection_plan_call('get', connection_id, plan_id)


@router.post("/{connection_id}/plans/{plan_id}/apply")
def apply_connection_plan(connection_id: str, plan_id: str, payload: dict[str, Any] = Body(...)):
    return connection_plan_call('apply', connection_id, plan_id, payload.get('version'), payload.get('confirmed'))


@router.post("/{connection_id}/plans/{plan_id}/recover")
def recover_connection_plan(connection_id: str, plan_id: str, payload: dict[str, Any] = Body(...)):
    return connection_plan_call('recover', connection_id, plan_id, payload.get('confirmed'))


@router.post("/{connection_id}/one-click-connect")
def one_click_connect_endpoint(connection_id: str, request: Request, payload: dict[str, Any] = Body(default={})):
    """One-step Senrelay connection: discover history paths, enable history capture, and install MCP + skill + hook."""
    import hashlib
    from pathlib import Path
    from app.database import db
    from app.services.connection_plans import ConnectionPlans, public_plan
    from app.services.host_registry import HOST_DEFINITIONS
    from app.integrations.config_transactions import ConfigurationError
    from app.integrations.installer import _load_manifest

    connection_service.discover_connections()
    current = connection_service.get_connection(connection_id)
    if isinstance(payload.get("paths"), list):
        target_paths = payload.get("paths")
    else:
        existing_paths = current.get("paths") or []
        suggested_paths = (current.get("discovery") or {}).get("suggested_paths") or []
        target_paths = list(dict.fromkeys([*existing_paths, *suggested_paths]))
    connection_service.patch_connection(
        connection_id=connection_id,
        enabled=True,
        paused=False,
        paths=target_paths,
        confirmed=True,
    )

    plan_result = None
    conflict_error = None
    if connection_id in HOST_DEFINITIONS:
        try:
            home_dir = Path.home().resolve()
            cred_file = home_dir / ".contextcore" / "credentials" / f"{connection_id}.token"
            manifest = _load_manifest(connection_id, home_dir)
            if cred_file.is_file() and not cred_file.is_symlink() and manifest and manifest.get("token_hash"):
                raw_token = cred_file.read_text(encoding="utf-8").strip()
                digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest() if raw_token else ""
                if digest and digest == manifest.get("token_hash"):
                    with db() as conn:
                        row = conn.execute("SELECT token_hash FROM connections WHERE id = ?", (connection_id,)).fetchone()
                        if row and not row["token_hash"]:
                            conn.execute(
                                "UPDATE connections SET token_hash = ?, token_preview = 'configured' WHERE id = ?",
                                (digest, connection_id),
                            )
        except Exception:
            pass
        host_def = HOST_DEFINITIONS[connection_id]
        surface = payload.get("surface") if payload.get("surface") in host_def.surfaces else host_def.surfaces[0]
        capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), list) and payload.get("capabilities") else ["context_tools", "continuation", "lifecycle"]
        base_url = str(request.base_url).rstrip("/") if request else None
        cp = ConnectionPlans()
        try:
            plan = cp.create(connection_id, {"operation": "install", "surface": surface, "capabilities": capabilities}, base_url)
            applied = cp.apply(connection_id, plan["id"], plan["version"], True)
            plan_result = public_plan(applied)
        except ConfigurationError as exc:
            conflict_error = str(exc)
        except Exception as exc:
            conflict_error = str(exc)

    try:
        connection_service.sync_connection(connection_id, force=False)
    except Exception:
        pass

    updated_conn = connection_service.get_connection(connection_id)
    return {
        "ok": conflict_error is None,
        "status": "connected" if conflict_error is None else "partial_connected",
        "connection": updated_conn,
        "plan": plan_result,
        "warning": conflict_error,
    }


@router.post("/{connection_id}/one-click-disconnect")
def one_click_disconnect_endpoint(connection_id: str, request: Request, payload: dict[str, Any] = Body(default={})):
    """One-step Senrelay disconnection: remove installed MCP/skill/hook components and disable history capture."""
    from app.services.connection_plans import ConnectionPlans, public_plan
    from app.services.host_registry import HOST_DEFINITIONS

    plan_result = None
    if connection_id in HOST_DEFINITIONS:
        host_def = HOST_DEFINITIONS[connection_id]
        surface = payload.get("surface") if payload.get("surface") in host_def.surfaces else host_def.surfaces[0]
        capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), list) and payload.get("capabilities") else ["context_tools", "continuation", "lifecycle"]
        base_url = str(request.base_url).rstrip("/") if request else None
        cp = ConnectionPlans()
        try:
            plan = cp.create(connection_id, {"operation": "uninstall", "surface": surface, "capabilities": capabilities}, base_url)
            applied = cp.apply(connection_id, plan["id"], plan["version"], True)
            plan_result = public_plan(applied)
        except Exception:
            pass

    updated_conn = connection_service.patch_connection(
        connection_id=connection_id,
        enabled=False,
        paused=False,
        confirmed=True,
    )
    return {
        "ok": True,
        "status": "disconnected",
        "connection": updated_conn,
        "plan": plan_result,
    }


@router.get("", response_model=list[Connection])
def list_connections_endpoint():
    return connection_service.list_connections()


@router.post("/discover", response_model=list[Connection])
def discover_connections_endpoint():
    return connection_service.discover_connections()


@router.patch("/{connection_id}", response_model=Connection)
def patch_connection_endpoint(connection_id: str, req: ConnectionPatch):
    return connection_service.patch_connection(
        connection_id=connection_id,
        enabled=req.enabled,
        paused=req.paused,
        paths=req.paths,
        confirmed=req.confirmed,
    )


@router.post("/{connection_id}/sync", response_model=Connection)
def sync_connection_endpoint(connection_id: str):
    return connection_service.sync_connection(connection_id)


@router.get("/{connection_id}/setup")
def setup_connection_endpoint(connection_id: str, request: Request):
    base_url = str(request.base_url).rstrip("/") if request else None
    return connection_service.setup_connection(connection_id, base_url=base_url)


@router.post("/{connection_id}/install")
def install_connection_endpoint(connection_id: str, request: Request, payload: dict[str, Any] = Body(default={})):
    confirmed = payload.get("confirmed")
    if confirmed is not True:
        raise HTTPException(status_code=422, detail="confirmed must be True to execute installation")
    raise HTTPException(status_code=409, detail="Create an explicit capability plan and confirm it through the plan apply endpoint")

@router.post("/{connection_id}/uninstall")
def uninstall_connection_endpoint(connection_id: str, payload: dict[str, Any] = Body(default={})):
    confirmed = payload.get("confirmed")
    if confirmed is not True:
        raise HTTPException(status_code=422, detail="confirmed must be True to execute uninstallation")
    raise HTTPException(status_code=409, detail="Create an explicit capability removal plan before changing host files")


@router.post("/{connection_id}/token/revoke")
def revoke_token_endpoint(connection_id: str):
    return connection_service.revoke_agent_token(connection_id)


@router.post("/{connection_id}/hook")
def receive_hook_event_endpoint(
    connection_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
):
    conn_data = connection_service.get_connection(connection_id)
    # Collection pause must not revoke shared context reads; bearer remains enabled
    is_collection_paused = not conn_data.get("enabled") or bool(conn_data.get("paused"))

    # Authenticate per-agent token
    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    elif request.headers.get("x-agent-token"):
        token = request.headers.get("x-agent-token", "").strip()

    verified_agent = connection_service.verify_agent_token(token, purpose="lifecycle") if token else None
    if not verified_agent or verified_agent != connection_id:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid agent token for this connection hook")

    from app.adapters import parse_hook

    try:
        normalized_records = parse_hook(connection_id, payload, dict(request.headers))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to parse hook event: {str(e)}")

    processed_count = 0

    # Lifecycle payloads can report capture/checkpoints, never request context
    # injection or stand in for an explicit target-Agent/user confirmation.
    for record in normalized_records:
        from app.services.host_registry import record_observation
        record_observation(connection_id, "capture", detail={"event_name": record.get("event"), "transport": "http_hook"})

        # If collection is paused, skip ingesting turns/conversations into DB
        if is_collection_paused:
            continue

        # Do not persist empty lifecycle events as new tasks or conversations
        messages = record.get("messages", [])
        if not messages and isinstance(payload.get("messages"), list):
            messages = [
                m for m in payload["messages"]
                if isinstance(m, dict) and str(m.get("content") or "").strip()
            ]
        summary = record.get("process_summary") or ""
        has_summary = bool(
            (isinstance(summary, list) and len(summary) > 0)
            or (isinstance(summary, str) and summary.strip())
        )
        has_messages = bool(messages and len(messages) > 0)
        has_explicit_auto_mem = payload.get("auto_summarize") is True or bool(
            isinstance(payload.get("memories"), list) and payload.get("memories")
        )
        is_empty_lifecycle = not has_messages and not has_summary and not has_explicit_auto_mem

        ext_id = record.get("external_id")
        source_p = record.get("source_path")
        cwd = record.get("cwd")

        if is_empty_lifecycle:
            with db() as conn:
                existing_row = None
                if ext_id:
                    existing_row = conn.execute(
                        "SELECT id FROM conversations WHERE agent = ? AND external_id = ?",
                        (connection_id, ext_id),
                    ).fetchone()
                if not existing_row and source_p:
                    existing_row = conn.execute(
                        "SELECT id FROM conversations WHERE agent = ? AND source_path = ?",
                        (connection_id, source_p),
                    ).fetchone()
            if not existing_row:
                continue

        # Title protection: do not overwrite title of full history with generic hook title
        incoming_title = (record.get("title") or "").strip()
        generic_prefixes = (
            f"{connection_id} Event",
            f"{connection_id.title()} Event",
            f"OMP {record.get('session_id')}",
            f"Codex {record.get('session_id')}",
            f"Cursor {record.get('session_id')}",
            f"Antigravity {record.get('session_id')}",
            f"Claude Code {record.get('session_id')}",
            f"OpenCode {record.get('session_id')}",
            "cursor:sessionStart",
        )
        is_generic = (
            not incoming_title
            or incoming_title.startswith(f"{connection_id} Event")
            or incoming_title.startswith(f"{connection_id.title()} Event")
            or any(incoming_title == p for p in generic_prefixes)
        )

        title = incoming_title
        with db() as conn:
            existing_conv = None
            if ext_id:
                existing_conv = conn.execute(
                    "SELECT id, title FROM conversations WHERE agent = ? AND external_id = ?",
                    (connection_id, ext_id),
                ).fetchone()
            if not existing_conv and source_p:
                existing_conv = conn.execute(
                    "SELECT id, title FROM conversations WHERE agent = ? AND source_path = ?",
                    (connection_id, source_p),
                ).fetchone()

        if existing_conv and is_generic and existing_conv["title"]:
            title = existing_conv["title"]
        elif not title:
            title = f"{connection_id} Event"

        # Stable source tombstone check
        tombstone_key = conversation_service.compute_source_identity_key(connection_id, source_p, title, ext_id)
        with db() as conn:
            tb = conn.execute(
                "SELECT id FROM tombstones WHERE entity_type = 'conversation' AND dedupe_key = ?",
                (tombstone_key,),
            ).fetchone()
        if tb:
            continue

        # Reconstruct or resolve project (pass metadata name ONLY when explicitly provided)
        proj_meta = {"name": record.get("project_name")} if record.get("project_name") else None
        event_activity_at = record.get("updated_at") or record.get("started_at") or record.get("timestamp")
        project_id = project_service.reconstruct_or_resolve_project(
            cwd,
            metadata=proj_meta,
            activity_at=event_activity_at,
        )

        conv = conversation_service.upsert_conversation(
            agent=connection_id,
            title=title,
            external_id=ext_id,
            project_id=project_id,
            cwd=cwd,
            source_path=source_p,
            messages=messages,
            process_summary=summary,
            incremental=True,
            started_at=record.get("started_at") or record.get("timestamp"),
            updated_at=record.get("updated_at") or record.get("timestamp"),
        )
        if conv.get("_changed", True):
            processed_count += 1

        from app.services import distillation_service

        explicit_auto = payload.get("auto_summarize") is True or isinstance(payload.get("memories"), list)
        implicit_extract = (
            distillation_service.extract_implicit_memories_from_messages(messages)
            if (not explicit_auto and payload.get("auto_summarize") is not False and has_messages and conv.get("_changed", True))
            else {}
        )
        has_implicit_work = bool(implicit_extract.get("memories") or implicit_extract.get("temp_constraints"))
        if explicit_auto or has_implicit_work:
            auto_memory_res = distillation_service.auto_summarize_memory(
                project_id=project_id,
                conversation_id=conv["id"],
                session_summary=payload.get("session_summary") or (summary if isinstance(summary, str) and summary.strip() else None),
                memories=payload.get("memories") if isinstance(payload.get("memories"), list) else None,
                messages=messages,
                cwd=cwd,
                actor=connection_id,
                auto_extract_from_messages=True,
            )
            record["_auto_memory"] = auto_memory_res

    response: dict[str, Any] = {
        "ok": True,
        "processed": processed_count,
        "context_type": "none",
    }
    for record in normalized_records:
        if record.get("_auto_memory"):
            receipt = record["_auto_memory"]
            response["auto_memory"] = {
                key: receipt[key] for key in (
                    "ok", "conversation_id", "saved_count", "created_count", "updated_count",
                    "unchanged_count", "skipped_count", "summary_updated",
                ) if key in receipt
            }
            break

    return response
