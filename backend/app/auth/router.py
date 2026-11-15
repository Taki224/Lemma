from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.auth.security import (
    LoginRateLimiter,
    PasswordVerifier,
    SecretRedactingFilter,
    resolve_client_ip,
)
from app.auth.session import (
    SESSION_COOKIE_NAME,
    SessionManager,
    SessionRecord,
    require_authenticated_session,
    require_csrf,
)
from app.config import AppSettings, save_settings_yaml
from app.config_secrets import SecretsStore

auth_router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class RotateSecretRequest(BaseModel):
    new_secret: str | None = None


class UpdateSecretsRequest(BaseModel):
    openrouter_api_key: str | None = None
    zotero_api_key: str | None = None
    zotero_library_id: str | None = None
    zotero_library_type: Literal["user", "group"] | None = None
    unpaywall_email: str | None = None


class ConsentRequest(BaseModel):
    accepted: bool = True


@auth_router.post("/api/auth/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
) -> dict[str, object]:
    settings: AppSettings = request.app.state.settings
    rate_limiter: LoginRateLimiter = request.app.state.rate_limiter
    verifier: PasswordVerifier = request.app.state.password_verifier
    session_manager: SessionManager = request.app.state.session_manager
    redactor: SecretRedactingFilter = request.app.state.secret_redactor

    client_ip = resolve_client_ip(request, settings.trusted_proxy_ips)
    allowed, retry_after = rate_limiter.check_allowed(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please wait before retrying.",
            headers={"Retry-After": str(retry_after)},
        )

    is_valid = await asyncio.to_thread(verifier.verify_password, payload.password)
    rate_limiter.record_result(client_ip, success=is_valid)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid workspace password.",
        )

    session_record = await asyncio.to_thread(session_manager.create_session)
    redactor.register_secrets([session_record.token, session_record.csrf_token])
    session_manager.set_session_cookie(response, session_record.token, request)

    return {
        "authenticated": True,
        "csrf_token": session_record.csrf_token,
        "insecure_http_warning": session_manager.is_insecure_non_localhost_http(request),
        "privacy_consent_accepted": settings.privacy_consent_accepted,
    }


@auth_router.post(
    "/api/auth/logout",
    dependencies=[Depends(require_csrf)],
)
async def logout(
    request: Request,
    response: Response,
    session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, bool]:
    session_manager: SessionManager = request.app.state.session_manager
    await asyncio.to_thread(session_manager.revoke_session, session.session_id)
    session_manager.clear_session_cookie(response, request)
    return {"authenticated": False}


@auth_router.post(
    "/api/auth/rotate-secret",
    dependencies=[Depends(require_authenticated_session), Depends(require_csrf)],
)
async def rotate_secret(
    request: Request,
    response: Response,
    payload: RotateSecretRequest | None = None,
) -> dict[str, bool]:
    session_manager: SessionManager = request.app.state.session_manager
    redactor: SecretRedactingFilter = request.app.state.secret_redactor
    try:
        new_sec = await asyncio.to_thread(
            session_manager.rotate_secret,
            payload.new_secret if payload else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    redactor.register_secrets([new_sec])
    session_manager.clear_session_cookie(response, request)
    return {"rotated": True}


@auth_router.get("/api/auth/status")
async def get_auth_status(request: Request) -> dict[str, object]:
    settings: AppSettings = request.app.state.settings
    session_manager: SessionManager = request.app.state.session_manager
    secrets_store: SecretsStore = request.app.state.secrets_store

    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    session_record = await asyncio.to_thread(session_manager.validate_token, raw_cookie)
    masked = secrets_store.get_masked_status()

    return {
        "authenticated": session_record is not None,
        "csrf_token": session_record.csrf_token if session_record is not None else None,
        "insecure_http_warning": session_manager.is_insecure_non_localhost_http(request),
        "privacy_consent_accepted": settings.privacy_consent_accepted,
        "secrets": masked,
        **masked,
    }


@auth_router.put(
    "/api/settings/secrets",
    dependencies=[Depends(require_authenticated_session), Depends(require_csrf)],
)
async def update_secrets(
    payload: UpdateSecretsRequest,
    request: Request,
) -> dict[str, object]:
    secrets_store: SecretsStore = request.app.state.secrets_store
    redactor: SecretRedactingFilter = request.app.state.secret_redactor

    updates = payload.model_dump(exclude_unset=True)
    await asyncio.to_thread(secrets_store.update_secrets, updates)
    redactor.register_secrets(secrets_store.get_redactable_values())

    masked = secrets_store.get_masked_status()
    return {
        "updated": True,
        "secrets": masked,
        **masked,
    }


@auth_router.post(
    "/api/settings/consent",
    dependencies=[Depends(require_authenticated_session), Depends(require_csrf)],
)
async def update_privacy_consent(
    payload: ConsentRequest,
    request: Request,
) -> dict[str, bool]:
    settings: AppSettings = request.app.state.settings
    settings.privacy_consent_accepted = bool(payload.accepted)
    await asyncio.to_thread(save_settings_yaml, settings)
    return {"privacy_consent_accepted": settings.privacy_consent_accepted}
