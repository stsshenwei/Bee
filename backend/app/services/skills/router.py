from __future__ import annotations

import json
from typing import Callable

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, StrictBool

from app.services.marketplace.marketplace_models import (
    MarketplaceAuthError,
    MarketplaceConflictError,
    MarketplaceForbiddenError,
    MarketplaceNotFoundError,
    MarketplaceValidationError,
)

from .models import SkillError, SkillValidationError


class _SkillRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request: Request):
            try:
                return await original(request)
            except SkillError as exc:
                return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "message": str(exc)})
            except MarketplaceAuthError as exc:
                return JSONResponse(status_code=401, content={"code": "skill_unauthorized", "message": str(exc)})
            except MarketplaceForbiddenError as exc:
                return JSONResponse(status_code=403, content={"code": "skill_forbidden", "message": str(exc)})
            except MarketplaceNotFoundError as exc:
                return JSONResponse(status_code=404, content={"code": "skill_not_found", "message": str(exc)})
            except MarketplaceConflictError as exc:
                return JSONResponse(status_code=409, content={"code": "skill_conflict", "message": str(exc)})
            except MarketplaceValidationError as exc:
                return JSONResponse(status_code=422, content={"code": "skill_invalid", "message": str(exc)})
        return handler


class SkillStatusBody(BaseModel):
    status: str


class SkillActivationBody(BaseModel):
    enabled: StrictBool
    version: str


def _metadata(raw: str) -> dict:
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise SkillValidationError("metadata exceeds 64 KiB")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SkillValidationError("metadata must be valid JSON") from exc
    if not isinstance(value, dict):
        raise SkillValidationError("metadata must be a JSON object")
    return value


async def _bounded_upload(file: UploadFile, service) -> bytes:
    data = await file.read(service.settings.max_upload_bytes + 1)
    if len(data) > service.settings.max_upload_bytes:
        from .models import SkillTooLargeError
        raise SkillTooLargeError("upload exceeds configured size")
    return data


def create_skill_router(get_service: Callable, get_marketplace: Callable, validate_workspace: Callable, get_runtime_enabled: Callable) -> APIRouter:
    router=APIRouter(route_class=_SkillRoute)
    def principal(auth, marketplace): return marketplace.resolve_principal(auth)
    @router.get("/skills")
    def listing(q:str="",category:str="",author:str="",sort:str="updated",cursor:int=0,limit:int=Query(20,ge=1,le=100),service=Depends(get_service)): return service.list_skills(q,category,author,sort,cursor,limit)
    @router.post("/skills/validate")
    async def validate(file:UploadFile=File(...),metadata:str=Form("{}"),authorization:str|None=Header(None),service=Depends(get_service),marketplace=Depends(get_marketplace)):
        p=principal(authorization,marketplace); marketplace.require_principal(p)
        return service.validate(await _bounded_upload(file, service), file.filename or "SKILL.md", _metadata(metadata))
    @router.post("/skills")
    async def publish(file:UploadFile=File(...),metadata:str=Form("{}"),authorization:str|None=Header(None),service=Depends(get_service),marketplace=Depends(get_marketplace)):
        p=principal(authorization,marketplace); marketplace.require_principal(p)
        return service.publish(await _bounded_upload(file, service), file.filename or "SKILL.md", _metadata(metadata), p)
    @router.get("/skills/{skill_id}")
    def detail(skill_id:str,authorization:str|None=Header(None),service=Depends(get_service),marketplace=Depends(get_marketplace)):
        return service.detail(skill_id, principal(authorization, marketplace))
    @router.post("/skills/{skill_id}/versions")
    async def publish_version(skill_id:str,file:UploadFile=File(...),metadata:str=Form("{}"),authorization:str|None=Header(None),service=Depends(get_service),marketplace=Depends(get_marketplace)):
        p=principal(authorization,marketplace); marketplace.require_principal(p)
        return service.publish(await _bounded_upload(file, service), file.filename or "SKILL.md", _metadata(metadata), p, skill_id)
    @router.get("/skills/{skill_id}/versions/{version}")
    def version(skill_id:str,version:str,authorization:str|None=Header(None),service=Depends(get_service),marketplace=Depends(get_marketplace)):
        return service.version(skill_id, version, principal(authorization, marketplace))
    @router.patch("/skills/{skill_id}/versions/{version}")
    def status(skill_id:str,version:str,body:SkillStatusBody,authorization:str|None=Header(None),service=Depends(get_service),marketplace=Depends(get_marketplace)):
        return service.set_status(skill_id,version,body.status,principal(authorization,marketplace))
    @router.get("/skills/{skill_id}/versions/{version}/files")
    def file_preview(skill_id:str,version:str,path:str,authorization:str|None=Header(None),service=Depends(get_service),marketplace=Depends(get_marketplace)):
        return service.preview(skill_id,version,path,principal(authorization,marketplace))
    @router.get("/skills/{skill_id}/versions/{version}/download")
    def download(skill_id:str,version:str,service=Depends(get_service)): return Response(service.download(skill_id,version),media_type="application/zip",headers={"Content-Disposition":f'attachment; filename="{skill_id}-{version}.zip"'})
    @router.get("/workspaces/{workspace_id}/skills")
    def workspace(workspace_id:str,service=Depends(get_service),runtime=Depends(get_runtime_enabled)):
        validate_workspace(workspace_id); return service.workspace_skills(workspace_id,runtime)
    @router.put("/workspaces/{workspace_id}/skills/{skill_id}")
    def activate(workspace_id:str,skill_id:str,body:SkillActivationBody,authorization:str|None=Header(None),service=Depends(get_service),marketplace=Depends(get_marketplace),runtime=Depends(get_runtime_enabled)):
        validate_workspace(workspace_id)
        return service.activate(workspace_id,skill_id,body.enabled,body.version,principal(authorization,marketplace),runtime)
    return router
