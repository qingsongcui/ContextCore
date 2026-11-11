"""Explicit file-selection migration, within existing browser security middleware."""
from fastapi import APIRouter, Body
from app.routers.manage import invoke
from app.services import migration_service

router=APIRouter(prefix='/api/manage/migrations',tags=['migration'])


@router.get('/sources')
def sources():
    return invoke(migration_service.service().sources)


@router.post('/plan')
def plan(body:dict=Body(...)):
    return invoke(migration_service.service().plan,body)


@router.get('/{plan_id}')
def get(plan_id:str):
    return invoke(migration_service.service().get,plan_id)


@router.post('/{plan_id}/apply')
def apply(plan_id:str,body:dict=Body(...)):
    return invoke(migration_service.service().apply,plan_id,confirmed=body.get('confirmed') is True,
                  import_secrets_confirmed=body.get('import_secrets_confirmed') is True)
