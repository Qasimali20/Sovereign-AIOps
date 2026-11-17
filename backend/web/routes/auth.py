from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.security import auth
from backend.core import store
from backend.web.security import COOKIE, Viewer, throttle

router = APIRouter(tags=["auth"])


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(min_length=8, max_length=256)


@router.post("/login")
def login(body: Login, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    throttle.check(ip)
    user = auth.check_password(body.username, body.password)
    if not user:
        throttle.fail(ip)
        store.audit(None, f"ip:{ip}", "login_failed", {"username": body.username})
        raise HTTPException(401, "wrong username or password")
    throttle.reset(ip)
    response.set_cookie(COOKIE, auth.make_session(user), max_age=auth.SESSION_TTL_S,
                        httponly=True, samesite="strict", path="/")
    store.audit(None, f"human:{user['username']}", "login", {"ip": ip})
    return user


@router.post("/logout")
def logout(request: Request, response: Response):
    auth.revoke_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: Viewer):
    return user


@router.post("/me/password")
def change_password(body: PasswordChange, user: Viewer):
    if user.get("via") == "token" or not auth.check_password(user["username"], body.current):
        raise HTTPException(400, "current password is wrong")
    auth.set_user(user["username"], body.new, user["role"])
    store.audit(None, f"human:{user['username']}", "password_changed", None)
    return {"ok": True}
