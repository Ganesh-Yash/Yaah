"""Auth endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status

from .. import audit, security
from ..config import Settings
from ..db import Database
from ..deps import client_ip, current_principal, get_db, get_settings
from ..errors import rate_limited
from ..models import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
)
from ..services import auth_service
from ..services.accounts_service import Principal

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _token_payload(pair: security.TokenPair) -> dict:
    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "token_type": "bearer",
        "expires_in": pair.access_expires_in,
        "refresh_expires_in": pair.refresh_expires_in,
    }


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    user = auth_service.register(
        db, settings, email=payload.email, password=payload.password, full_name=payload.full_name
    )
    return {"user": user}


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    ip = client_ip(request)
    if not request.app.state.login_limiter.allow(ip or "unknown"):
        raise rate_limited("Too many sign-in attempts from this address. Try again shortly.")
    pair, user = auth_service.login(
        db, settings, email=payload.email, password=payload.password, ip=ip
    )
    return {**_token_payload(pair), "user": user}


@router.post("/refresh")
def refresh(
    payload: RefreshRequest,
    request: Request,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    pair = auth_service.refresh(
        db, settings, refresh_token=payload.refresh_token, ip=client_ip(request)
    )
    return _token_payload(pair)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    payload: LogoutRequest,
    db: Database = Depends(get_db),
    principal: Principal = Depends(current_principal),
) -> Response:
    auth_service.logout(db, refresh_token=payload.refresh_token, actor=principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me")
def me(principal: Principal = Depends(current_principal)) -> dict:
    return {
        "user": {
            "id": principal.id,
            "email": principal.email,
            "full_name": principal.full_name,
            "role": principal.role,
            "status": principal.status,
        }
    }


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordRequest,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(current_principal),
) -> Response:
    auth_service.change_password(
        db,
        settings,
        user_id=principal.id,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
