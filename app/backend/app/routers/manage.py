"""Browser management API; inherits the app's Origin/CSRF/loopback boundary."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from app.integrations.config_transactions import ConfigurationError
from app.integrations.secret_store import SecretUnavailable
from app.services import management_service as management
from app.services import provider_presets

router = APIRouter(prefix="/api/manage", tags=["management"])


def invoke(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ConfigurationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except SecretUnavailable:
        raise HTTPException(status_code=503, detail="Keychain is unavailable or locked; no plaintext fallback was used") from None
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=422, detail="Invalid management payload") from None


def public_plan(plan):
    result = {**plan, "payload": {**plan["payload"], "files": []}}
    result['payload']['metadata'] = {key: value for key, value in plan['payload'].get('metadata', {}).items() if key != 'host_routing'}
    for item in plan["payload"]["files"]:
        result["payload"]["files"].append({key: value for key, value in item.items() if key not in ("specs", "snapshot", "changes")})
    return result


@router.get("/status")
def status():
    return invoke(management.service().status)


@router.get("/hosts")
def hosts():
    return invoke(management.service().hosts)


@router.get("/provider-presets")
def provider_preset_list():
    return provider_presets.list_presets()


@router.get("/hosts/{host_id}/state")
def host_configuration_state(host_id: str):
    return invoke(management.service().host_state, host_id)


@router.get('/hosts/{host_id}/mode')
def host_connection_mode(host_id: str):
    return invoke(management.service().connection_mode, host_id)


@router.post('/hosts/{host_id}/mode/preview')
def preview_connection_mode(host_id: str, request: Request, body: dict = Body(...)):
    return public_plan(invoke(management.service().preview_connection, host_id, body, str(request.base_url)))


@router.post("/providers/setup")
def setup_provider(body: dict = Body(...)):
    return invoke(management.service().setup_provider, body)


@router.post("/providers/setup/revise")
def revise_provider_setup(body: dict = Body(...)):
    return invoke(management.service().revise_provider_setup, body)


@router.post("/credentials/cleanup")
def cleanup_credentials():
    return invoke(management.service().retry_credential_cleanup)


@router.get("/export")
def export_all():
    return invoke(management.service().export)


@router.post("/import")
def import_all(body: dict = Body(...)):
    return invoke(management.service().import_objects, body)


@router.post("/assets/import")
def import_assets(body: dict = Body(...)):
    return invoke(management.service().import_objects, body)


@router.post("/assets/discover")
def discover_assets(body: dict = Body(...)):
    return invoke(management.service().discover_assets, body.get("host_id"))


@router.get("/assets/export")
def export_assets(kind: str | None = Query(None)):
    result = invoke(management.service().export, "asset")
    if kind:
        result["objects"] = [entry for entry in result["objects"] if entry["data"]["kind"] == kind]
    return result


@router.get("/providers/export")
def export_providers():
    return invoke(management.service().export, "provider")


@router.get("/env-conflicts")
def check_env_conflicts(host_id: str | None = Query(None)):
    return invoke(management.service().check_env_conflicts, host_id)


@router.post("/providers/reorder")
def reorder_providers(body: dict = Body(...)):
    return invoke(management.service().reorder_providers, body.get("ordered_ids"))


@router.post("/hosts/{host_id}/quick-switch")
def quick_switch_provider(host_id: str, body: dict = Body(...)):
    result = invoke(
        management.service().quick_switch_provider,
        host_id,
        body.get("provider_id"),
        body.get("model_id"),
        auto_apply=bool(body.get("auto_apply", False)),
    )
    if isinstance(result, dict) and isinstance(result.get("plan"), dict):
        result = {**result, "plan": public_plan(result["plan"])}
    return result


@router.post("/hosts/{host_id}/quick-toggle-asset")
def quick_toggle_asset(host_id: str, body: dict = Body(...)):
    result = invoke(
        management.service().quick_toggle_asset,
        host_id,
        body.get("asset_id"),
        body.get("enabled", True),
        auto_apply=bool(body.get("auto_apply", False)),
    )
    if isinstance(result, dict) and isinstance(result.get("plan"), dict):
        result = {**result, "plan": public_plan(result["plan"])}
    return result


@router.post("/sync-hosts")
def sync_to_hosts(body: dict = Body(...)):
    result = invoke(
        management.service().sync_to_hosts,
        body.get("kind"),
        body.get("entity_id"),
        body.get("host_ids"),
        str(body.get("operation") or "install"),
        body.get("model_id"),
        auto_apply=bool(body.get("auto_apply", False)),
    )

    result["results"] = {
        host: {**entry, "plan": public_plan(entry["plan"])} if isinstance(entry.get("plan"), dict) else entry
        for host, entry in result["results"].items()
    }
    return result


@router.post("/sync-local")
def sync_local_environment():
    return invoke(management.service().sync_local_environment)



def register_crud(path: str, kind: str):
    def listing(summary: bool = Query(False), agent_id: str | None = Query(None),
                resource_kind: str | None = Query(None, alias='kind'), provider_id: str | None = Query(None),
                q: str = Query('', max_length=200), scope: str = Query('assigned'),
                limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), ids: str | None = Query(None, max_length=10100),
                include_unbound: bool = Query(False)):
        manager = management.service()
        if summary or agent_id or resource_kind or provider_id is not None or q or offset or limit != 50 or scope != 'assigned' or ids is not None or include_unbound:
            return invoke(manager.catalog_page, kind, agent_id=agent_id, resource_kind=resource_kind,
                          provider_id=provider_id, query=q, scope=scope, limit=limit, offset=offset,
                          entity_ids=ids.split(',') if ids else ([] if ids is not None else None), include_unbound=include_unbound)
        return invoke(manager.list, kind)

    def create(body: dict = Body(...)):
        return invoke(management.service().save, kind, body)

    def get(entity_id: str):
        manager = management.service()
        return invoke(manager._public, invoke(manager.get, kind, entity_id))

    def patch(entity_id: str, body: dict = Body(...)):
        return invoke(management.service().save, kind, body, entity_id, body.get("expected_version"))

    def delete(entity_id: str, expected_version: int | None = Query(None)):
        return invoke(management.service().delete, kind, entity_id, expected_version)

    def assignment(entity_id: str, body: dict = Body(...)):
        return invoke(management.service().assign_agent, kind, entity_id, body.get('host_id'), body.get('selected'), body.get('expected_version'))

    router.add_api_route(path, listing, methods=["GET"], name=f"list_management_{kind}")
    router.add_api_route(path, create, methods=["POST"], name=f"create_management_{kind}")
    router.add_api_route(path + "/{entity_id}", get, methods=["GET"], name=f"get_management_{kind}")
    router.add_api_route(path + "/{entity_id}", patch, methods=["PATCH"], name=f"patch_management_{kind}")
    router.add_api_route(path + "/{entity_id}", delete, methods=["DELETE"], name=f"delete_management_{kind}")
    router.add_api_route(path + "/{entity_id}/assignment", assignment, methods=["POST"], name=f"assign_management_{kind}")


for plural, kind in (("providers", "provider"), ("models", "model"), ("bindings", "binding"), ("accounts", "account"), ("assets", "asset"), ("bundles", "bundle")):
    register_crud("/" + plural, kind)


@router.post("/providers/{entity_id}/credential")
def provider_credential(entity_id: str, body: dict = Body(...)):
    return invoke(management.service().credential, "provider", entity_id, body.get("secret"))


@router.post("/accounts/{entity_id}/credential")
def account_credential(entity_id: str, body: dict = Body(...)):
    return invoke(management.service().credential, "account", entity_id, body.get("secret"))


@router.delete("/providers/{entity_id}/credential")
def remove_provider_credential(entity_id: str):
    return invoke(management.service().remove_credential, "provider", entity_id)


@router.delete("/accounts/{entity_id}/credential")
def remove_account_credential(entity_id: str):
    return invoke(management.service().remove_credential, "account", entity_id)


@router.post("/accounts/{entity_id}/{action}")
async def account_action(entity_id: str, action: str, request: Request, body: dict = Body(default={})):
    from app.services.oauth_service import OAuthService, CALLBACK_PATH
    manager = management.service()
    account = invoke(manager.get, "account", entity_id)
    if account["kind"] == "oauth" and action in ("authorize", "refresh", "revoke") and account.get("provider_id"):
        oauth = OAuthService(manager)
        try:
            if action == "authorize":
                return oauth.authorize(entity_id, str(request.base_url).rstrip("/") + CALLBACK_PATH)
            if action == "revoke":
                return await oauth.revoke(entity_id, body.get("confirmed") is True)
            return await oauth.refresh(entity_id)
        except ConfigurationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
        except SecretUnavailable:
            raise HTTPException(status_code=503, detail="Keychain is unavailable; OAuth credentials were not read") from None
    return invoke(manager.account_action, entity_id, action)


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(request: Request):
    from app.services.oauth_service import OAuthService
    params = request.query_params
    # Uvicorn logs after the handler completes; remove sensitive query material
    # from its scope before any exchange. Do not echo code/error descriptions.
    request.scope["query_string"] = b""
    if len(params.getlist("state")) != 1 or len(params.getlist("code")) > 1 or len(params.getlist("error")) > 1:
        raise HTTPException(status_code=400, detail="Invalid OAuth callback")
    try:
        await OAuthService(management.service()).callback(params.get("state"), params.get("code"), params.get("error"))
    except (ConfigurationError, SecretUnavailable):
        return HTMLResponse("<script>history.replaceState(null,'','/')</script><p>Authorization could not be completed. Return to Senrelay and restart the official login.</p>", status_code=400, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    return HTMLResponse("<script>history.replaceState(null,'','/')</script><p>Authorization completed. Return to Senrelay.</p>", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/assets/{entity_id}/versions")
def asset_versions(entity_id: str, summary: bool = Query(False), limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    if summary or offset or limit != 20:
        return invoke(management.service().asset_version_page, entity_id, limit, offset)
    return invoke(management.service().asset_versions, entity_id)


@router.get("/assets/{entity_id}/versions/{version}")
def asset_revision(entity_id: str, version: int):
    return invoke(management.service().asset_revision, entity_id, version)


@router.post("/assets/{entity_id}/restore")
def restore_asset(entity_id: str, body: dict = Body(...)):
    return invoke(management.service().restore_asset, entity_id, body.get("version"), body.get("expected_version"))


@router.post("/assets/{entity_id}/probe")
async def probe_asset(entity_id: str, body: dict = Body(...)):
    try:
        return await management.service().probe_asset(entity_id, body.get("confirmed") is True, expected_version=body.get("expected_version"))
    except ConfigurationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except SecretUnavailable:
        raise HTTPException(status_code=503, detail="Keychain is unavailable; the MCP credential was not sent") from None


@router.get("/plans")
def plans(summary: bool = Query(False), agent_id: str | None = Query(None), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    if summary or agent_id or offset or limit != 50:
        return invoke(management.service().plan_page, agent_id, limit, offset)
    return [public_plan(plan) for plan in invoke(management.service().transactions.list)]


@router.post("/plans")
def plan_configuration(body: dict = Body(...)):
    return public_plan(invoke(management.service().plan, body))


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str):
    return public_plan(invoke(management.service().transactions.get, plan_id))


@router.post("/plans/{plan_id}/apply")
def apply_plan(plan_id: str, body: dict = Body(...)):
    return public_plan(invoke(management.service().apply, plan_id, body.get("expected_version"), body.get("confirmed") is True))


@router.post("/plans/{plan_id}/recover")
def recover_plan(plan_id: str, body: dict = Body(...)):
    return public_plan(invoke(management.service().recover_plan, plan_id, body.get("confirmed") is True))
