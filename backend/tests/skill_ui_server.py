"""Disposable browser smoke fixture: real skill API, no corpus or model calls."""
import tempfile
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from app.services.marketplace import MarketplacePrincipal
from app.services.skills import SkillService, InMemorySkillRepository, create_skill_router

temp = tempfile.TemporaryDirectory(prefix="bee-skill-ui-")
service = SkillService(InMemorySkillRepository(), Path(temp.name))
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class Market:
    def resolve_principal(self, value):
        return MarketplacePrincipal("test-publisher", ("admin",)) if value == "Bearer ui-test-token" else None

    def require_principal(self, principal):
        if principal is None:
            raise HTTPException(401, "test token required")
        return principal


def workspace(value):
    if value != "test-workspace":
        raise HTTPException(404, "workspace not found")


app.include_router(create_skill_router(lambda: service, lambda: Market(), workspace, lambda: True))


@app.get("/workspaces/default")
def default_workspace():
    return {"id": "test-workspace", "name": "技能验收工作空间"}


@app.get("/api/v1/sessions/recent")
@app.get("/knowledge-bases")
@app.get("/memories")
def empty():
    return {"items": []}
