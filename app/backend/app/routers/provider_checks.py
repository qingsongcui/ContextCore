from fastapi import APIRouter, Body, HTTPException
from app.services import provider_checks as service
from app.integrations.config_transactions import ConfigurationError
from app.integrations.secret_store import SecretUnavailable
from app.routers.manage import invoke

router = APIRouter(prefix="/api/manage/providers", tags=["provider checks"])


@router.get("/{provider_id}/models/catalog")
def catalog(provider_id: str):
    return invoke(service.catalog, provider_id)


@router.post("/{provider_id}/models/catalog")
async def refresh_catalog(provider_id: str, data: dict = Body(default={})):
    if set(data) - {"after_id"}:
        raise HTTPException(422, "Expected optional after_id only")
    try:
        return await service.refresh_catalog(provider_id, after_id=data.get("after_id"))
    except ConfigurationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except SecretUnavailable:
        raise HTTPException(503, "Keychain is unavailable or locked; no plaintext fallback was used") from None


@router.post("/{provider_id}/check")
async def check(provider_id: str, data: dict = Body(...)):
    try:
        return await service.check(provider_id, data)
    except ConfigurationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except SecretUnavailable:
        raise HTTPException(503, "Keychain is unavailable or locked; no plaintext fallback was used") from None
