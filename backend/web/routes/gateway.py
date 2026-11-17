"""Console view of the model gateway: live routing/health stats and API key management."""
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core import config, store
from backend.llmops.gateway import keys
from backend.web.security import Admin, Viewer

router = APIRouter(prefix="/gateway", tags=["gateway"])
PROTECTED = {"aiops-platform"}          # the platform's own key: revoking it would cut the SRE agent off


@router.get("")
def gateway_stats(user: Viewer):
    try:
        r = httpx.get(f"{config.GATEWAY_URL}/gateway/stats", timeout=3,
                      headers={"Authorization": f"Bearer {config.GATEWAY_KEY}"})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"gateway unreachable: {type(e).__name__}") from e
    return r.json() | {"public_url": f"http://{config.NODE_BY_NAME['Node-2'].lan_ip}:{config.GATEWAY_PORT}/v1"}


class NewKey(BaseModel):
    name: str = Field(min_length=2, max_length=48, pattern=r"^[A-Za-z0-9 ._-]+$")


@router.get("/keys")
def list_keys(user: Admin):
    return [k | {"protected": k["name"] in PROTECTED} for k in keys.list_keys()]


@router.post("/keys")
def create_key(body: NewKey, user: Admin):
    if body.name in PROTECTED:
        raise HTTPException(400, "reserved name")
    key, meta = keys.create(body.name, user["username"])
    store.audit(None, f"human:{user['username']}", "gateway_key_created", {"id": meta["id"], "name": meta["name"]})
    return {"key": key, **meta}


@router.post("/keys/{kid}/revoke")
def revoke_key(kid: str, user: Admin):
    meta = next((k for k in keys.list_keys() if k["id"] == kid), None)
    if not meta:
        raise HTTPException(404)
    if meta["name"] in PROTECTED:
        raise HTTPException(400, "the platform's own key cannot be revoked here")
    keys.revoke(kid)
    store.audit(None, f"human:{user['username']}", "gateway_key_revoked", {"id": kid, "name": meta["name"]})
    return {"ok": True}
