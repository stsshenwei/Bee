import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.marketplace import MarketplacePrincipal, MarketplaceService, MarketplaceSettings, MarketplaceStorage
from app.services.marketplace.memory_marketplace_repository import InMemoryMarketplaceRepository
from app.services.skills import InMemorySkillRepository, SkillService, create_skill_router


class Market:
    def resolve_principal(self, value):
        return MarketplacePrincipal("admin",("admin",)) if value=="Bearer token" else None
    def require_principal(self, principal):
        from app.services.skills.models import SkillAuthError
        if principal is None: raise SkillAuthError("token required")
        return principal


def test_injected_router_publish_activate_and_errors():
    with tempfile.TemporaryDirectory() as root:
        service=SkillService(InMemorySkillRepository(),Path(root)/"library")
        app=FastAPI()
        app.include_router(create_skill_router(lambda:service,lambda:Market(),lambda wid: wid=="w1",lambda:True))
        client=TestClient(app)
        md=b"---\nname: route-skill\ndescription: route test\n---\nBody"
        unauthorized=client.post("/skills/validate",files={"file":("SKILL.md",md)})
        assert unauthorized.status_code==401
        published=client.post("/skills",files={"file":("SKILL.md",md),"metadata":(None,'{"version":"1.0.0"}')},headers={"Authorization":"Bearer token"})
        assert published.status_code==200, published.text
        sid=published.json()["skill_id"]
        activated=client.put(f"/workspaces/w1/skills/{sid}",json={"enabled":True,"version":"1.0.0"},headers={"Authorization":"Bearer token"})
        assert activated.status_code==200
        assert client.get("/workspaces/w1/skills").json()["items"][0]["available"] is True
        download=client.get(f"/skills/{sid}/versions/1.0.0/download")
        assert download.status_code==200 and download.content.startswith(b"PK")


def test_router_maps_real_marketplace_auth_and_invalid_metadata():
    with tempfile.TemporaryDirectory() as root:
        settings = MarketplaceSettings(storage_dir=str(Path(root) / "market"))
        market = MarketplaceService(InMemoryMarketplaceRepository(), MarketplaceStorage(settings.storage_dir), settings)
        service = SkillService(InMemorySkillRepository(), Path(root) / "skills")
        app = FastAPI()
        app.include_router(create_skill_router(lambda: service, lambda: market, lambda wid: True, lambda: True))
        client = TestClient(app)
        md = b"---\nname: x\ndescription: x\n---\nbody"
        missing = client.post("/skills/validate", files={"file": ("SKILL.md", md)})
        market.repository.upsert_token("publish", "alice", scopes=("publish",))
        invalid = client.post(
            "/skills/validate",
            files={"file": ("SKILL.md", md), "metadata": (None, "[]")},
            headers={"Authorization": "Bearer publish"},
        )
        wrong_bool = client.put(
            "/workspaces/w1/skills/missing",
            json={"enabled": "yes", "version": "1.0.0"},
            headers={"Authorization": "Bearer publish"},
        )
        assert missing.status_code == 401
        assert invalid.status_code == 422
        assert wrong_bool.status_code == 422
