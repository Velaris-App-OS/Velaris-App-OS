"""Auth API router — production credential-only login."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str
    mfa_token: str | None = None
    device_id: str | None = None   # Group J: reuse this browser's device row


class LoginResponse(BaseModel):
    access_token:  str
    refresh_token: str = ""
    token_type:    str = "bearer"
    expires_in:    int = 0
    user:          dict
    mfa_required:  bool = False
    device_id:     str = ""        # Group J


class UserInfoResponse(BaseModel):
    user_id: str
    username: str
    email: str
    roles: list[str]
    groups: list[str]
    is_admin: bool
    is_designer: bool
    is_case_worker: bool
    # Active access group + its privilege list, so the UI can gate controls on
    # the same privileges the backend enforces (e.g. case_type update/delete).
    active_access_group: dict | None = None


@router.post("/login", response_model=LoginResponse)
async def login(request: Request, body: LoginRequest, session: AsyncSession = Depends(get_session)):
    from case_service.api.routers.auth_real import login_real
    from case_service.api.routers.auth_real import LoginRequest as RealLoginReq
    result = await login_real(
        request,
        RealLoginReq(username=body.username, password=body.password,
                     mfa_token=body.mfa_token, device_id=body.device_id),
        session,
    )
    return LoginResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        user=result.user,
        mfa_required=result.mfa_required,
        device_id=result.device_id,
    )


@router.get("/me", response_model=UserInfoResponse)
async def get_me(user: AuthenticatedUser = Depends(get_current_user)):
    return UserInfoResponse(**user.to_dict())


@router.get("/roles")
async def list_available_roles():
    return {
        "roles": [
            {"id": "admin",       "name": "Administrator", "description": "Full system access"},
            {"id": "designer",    "name": "Case Designer",  "description": "Create and edit case types, forms, rules"},
            {"id": "case_worker", "name": "Case Worker",    "description": "Work on cases, complete assignments"},
            {"id": "manager",     "name": "Manager",        "description": "View analytics, manage queues"},
            {"id": "viewer",      "name": "Viewer",         "description": "Read-only access to cases"},
        ]
    }
