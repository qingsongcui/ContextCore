from fastapi import APIRouter, Body, HTTPException, Request
from app.services import routing_service as service

router = APIRouter(prefix="/api/routing", tags=["routing"])


@router.get("/status")
def status():
    return service.status()


@router.get("/settings")
def settings():
    return service.settings()


@router.patch("/settings")
def configure(data: dict = Body(...)):
    return service.configure(data)


@router.post("/token")
def token(data: dict = Body(...)):
    if set(data) != {"confirmed"}:
        raise HTTPException(422, "Expected confirmed only")
    return service.rotate_token(data["confirmed"])


@router.post("/v1/chat/completions")
async def chat(request: Request):
    return await service.forward(request, "chat/completions")


@router.post("/v1/responses")
async def responses(request: Request):
    return await service.forward(request, "responses")


@router.post("/v1/messages")
async def messages(request: Request):
    return await service.forward(request, "messages")


@router.get('/v1/hosts/{host_id}/models')
def host_models(host_id: str, request: Request):
    value = service.host_profile(host_id, request.headers)
    aliases = list(value['gateway']['model_map'])
    return {'object': 'list', 'data': [{'id': alias, 'object': 'model', 'owned_by': 'senrelay-local'} for alias in aliases]}


@router.post('/v1/hosts/{host_id}/chat/completions')
async def host_chat(host_id: str, request: Request):
    return await service.forward(request, 'chat/completions', host_id)


@router.post('/v1/hosts/{host_id}/responses')
async def host_responses(host_id: str, request: Request):
    return await service.forward(request, 'responses', host_id)


@router.post('/v1/hosts/{host_id}/messages')
async def host_messages(host_id: str, request: Request):
    return await service.forward(request, 'messages', host_id)
