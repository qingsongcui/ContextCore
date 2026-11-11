from fastapi import APIRouter, Body, Query
from app.services import usage_service as service

router = APIRouter(prefix="/api/usage", tags=["usage"])


def filters(start=None, end=None, host_id=None, provider_id=None, model=None, project_id=None, account_id=None, status=None, logical_request_id=None):
    return {k: v for k, v in locals().items() if v is not None}


@router.get("/summary")
def summary(start: str | None = None, end: str | None = None, tz: str = "Asia/Shanghai", host_id: str | None = None, provider_id: str | None = None, model: str | None = None, project_id: str | None = None, account_id: str | None = None, status: str | None = None, logical_request_id: str | None = None):
    return service.summary(filters(start, end, host_id, provider_id, model, project_id, account_id, status, logical_request_id), tz)


@router.get("/requests")
def requests(start: str | None = None, end: str | None = None, host_id: str | None = None, provider_id: str | None = None, model: str | None = None, project_id: str | None = None, account_id: str | None = None, status: str | None = None, logical_request_id: str | None = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    return service.requests(filters(start, end, host_id, provider_id, model, project_id, account_id, status, logical_request_id), limit, offset)


@router.get("/coverage")
def coverage(start: str | None = None, end: str | None = None, host_id: str | None = None, provider_id: str | None = None, model: str | None = None, project_id: str | None = None, account_id: str | None = None, status: str | None = None, logical_request_id: str | None = None):
    return service.coverage(filters(start, end, host_id, provider_id, model, project_id, account_id, status, logical_request_id))


@router.post("/ingest")
def ingest(data: dict = Body(...)):
    from fastapi import HTTPException
    if set(data) != {"observations"}:
        raise HTTPException(422, "Expected an explicit observations batch")
    return service.ingest(data["observations"])


@router.get("/prices")
def prices():
    return {"items": service.list_prices()}


@router.post("/prices")
def price(data: dict = Body(...)):
    return service.add_price(data)
