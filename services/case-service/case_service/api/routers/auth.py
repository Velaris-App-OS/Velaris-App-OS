"""Auth API router — production credential-only login."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
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


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict
    mfa_required: bool = False


class UserInfoResponse(BaseModel):
    user_id: str
    username: str
    email: str
    roles: list[str]
    groups: list[str]
    is_admin: bool
    is_designer: bool
    is_case_worker: bool


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)):
    from case_service.api.routers.auth_real import login_real
    from case_service.api.routers.auth_real import LoginRequest as RealLoginReq
    result = await login_real(
        RealLoginReq(username=body.username, password=body.password, mfa_token=body.mfa_token),
        session,
    )
    return LoginResponse(
        access_token=result.access_token,
        user=result.user,
        mfa_required=result.mfa_required,
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
