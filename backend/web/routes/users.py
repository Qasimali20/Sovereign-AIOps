from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.security import auth
from backend.core import config, store
from backend.web.security import Admin

router = APIRouter(tags=["users"])
RESERVED = set(config.NON_HUMAN_ACTORS)


class NewUser(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=8, max_length=256)
    role: str


@router.get("/users")
def users(user: Admin):
    return auth.list_users()


@router.post("/users")
def save_user(body: NewUser, user: Admin):
    if body.role not in auth.ROLES:
        raise HTTPException(400, f"role must be one of {auth.ROLES}")
    if body.username.lower() in RESERVED:
        raise HTTPException(400, "reserved name")
    if body.username == user["username"] and body.role != "admin":
        raise HTTPException(400, "you cannot remove your own admin role")
    existed = auth.get_user(body.username) is not None
    auth.set_user(body.username, body.password, body.role)
    store.audit(None, f"human:{user['username']}", "user_updated" if existed else "user_created",
                {"username": body.username, "role": body.role})
    return {"ok": True}


@router.post("/users/{username}/delete")
def delete_user(username: str, user: Admin):
    if username == user["username"]:
        raise HTTPException(400, "you cannot delete yourself")
    if not auth.delete_user(username):
        raise HTTPException(404)
    store.audit(None, f"human:{user['username']}", "user_deleted", {"username": username})
    return {"ok": True}
