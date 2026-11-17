from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.ai.model_registry import EnabledModel, ModelRegistry
from app.ai.openrouter import OpenRouterClient
from app.auth.session import SessionRecord, require_authenticated_session, require_csrf
from app.config import AppSettings, save_settings_yaml

_DEFAULT_CONTEXT_LENGTH = 128000
_DEFAULT_LOCAL_CONTEXT_LENGTH = 32768
_DEFAULT_PROMPT_PRICE = 0.000003
_DEFAULT_COMPLETION_PRICE = 0.000015

models_router = APIRouter(tags=["models"])
_smart_routing_update_lock = asyncio.Lock()
_chat_naming_update_lock = asyncio.Lock()
_web_search_update_lock = asyncio.Lock()
_local_config_update_lock = asyncio.Lock()


def _validate_local_base_url(raw_url: str) -> str:
    cleaned = raw_url.strip().rstrip("/")
    if not cleaned.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="Local model base URL must start with http:// or https://.",
        )
    return cleaned


class EnableModelRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    model_id: str = Field(min_length=1, max_length=200)
    supports_vision: bool | None = None
    provider: Literal["openrouter", "local"] = "openrouter"
    base_url: str = Field(default="", max_length=500)


@models_router.get("/api/ai/models")
async def list_enabled_models(
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    registry: ModelRegistry = request.app.state.model_registry
    models = await asyncio.to_thread(registry.list_enabled)
    return {"models": [asdict(m) for m in models]}


@models_router.get("/api/settings/models/catalog")
async def search_model_catalog(
    request: Request,
    q: str = Query("", max_length=200),
    limit: int = 25,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    openrouter: OpenRouterClient = request.app.state.openrouter_client
    bounded_limit = max(1, min(int(limit), 100))
    models = await openrouter.search_catalog(q, limit=bounded_limit)
    return {
        "models": [asdict(m) for m in models],
        "catalog_available": openrouter.catalog_is_live,
    }


@models_router.post("/api/settings/models", status_code=201)
async def enable_model(
    payload: EnableModelRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    registry: ModelRegistry = request.app.state.model_registry
    openrouter: OpenRouterClient = request.app.state.openrouter_client

    if payload.provider == "local":
        settings: AppSettings = request.app.state.settings
        resolved_base_url = _validate_local_base_url(
            payload.base_url or settings.local_model_base_url
        )
        local_catalog, _ = await openrouter.list_local_models(resolved_base_url)
        match = next((m for m in local_catalog if m.id == payload.model_id), None)
        if match is not None:
            model = EnabledModel(
                id=match.id,
                name=match.name,
                context_length=match.context_length,
                prompt_price_per_token=0.0,
                completion_price_per_token=0.0,
                supports_vision=(
                    payload.supports_vision
                    if payload.supports_vision is not None
                    else match.supports_vision
                ),
                verified=True,
                provider="local",
                base_url=resolved_base_url,
            )
        else:
            model = EnabledModel(
                id=payload.model_id,
                name=payload.model_id,
                context_length=_DEFAULT_LOCAL_CONTEXT_LENGTH,
                prompt_price_per_token=0.0,
                completion_price_per_token=0.0,
                supports_vision=payload.supports_vision or False,
                verified=False,
                provider="local",
                base_url=resolved_base_url,
            )
        openrouter.set_model_pricing(model.id, 0.0, 0.0)
        saved = await asyncio.to_thread(registry.enable, model)
        return asdict(saved)

    catalog = await openrouter.list_models()
    match = next((m for m in catalog if m.id == payload.model_id), None)
    if match is not None:
        model = EnabledModel(
            id=match.id,
            name=match.name,
            context_length=match.context_length,
            prompt_price_per_token=match.prompt_price_per_token,
            completion_price_per_token=match.completion_price_per_token,
            supports_vision=match.supports_vision,
            verified=True,
        )
    else:
        enabled = await asyncio.to_thread(registry.list_enabled)
        existing = next((m for m in enabled if m.id == payload.model_id), None)
        if existing is not None and existing.verified:
            return asdict(existing)
        model = EnabledModel(
            id=payload.model_id,
            name=payload.model_id,
            context_length=_DEFAULT_CONTEXT_LENGTH,
            prompt_price_per_token=_DEFAULT_PROMPT_PRICE,
            completion_price_per_token=_DEFAULT_COMPLETION_PRICE,
            supports_vision=payload.supports_vision or False,
            verified=False,
        )
        openrouter.set_model_pricing(
            model.id, model.prompt_price_per_token, model.completion_price_per_token
        )

    saved = await asyncio.to_thread(registry.enable, model)
    return asdict(saved)


@models_router.delete("/api/settings/models/{model_id:path}", status_code=204)
async def disable_model(
    model_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> Response:
    registry: ModelRegistry = request.app.state.model_registry
    removed = await asyncio.to_thread(registry.disable, model_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Model is not enabled.")
    return Response(status_code=204)


class SmartRoutingSettings(BaseModel):
    enabled: bool = False
    model: str = ""


def _smart_routing_payload(settings: AppSettings) -> dict[str, Any]:
    return {
        "enabled": settings.smart_routing_enabled,
        "model": settings.smart_routing_model,
    }


@models_router.get("/api/settings/smart-routing")
async def get_smart_routing(
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    return _smart_routing_payload(settings)


@models_router.put("/api/settings/smart-routing")
async def update_smart_routing(
    payload: SmartRoutingSettings,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    registry: ModelRegistry = request.app.state.model_registry
    model = payload.model.strip()
    async with _smart_routing_update_lock:
        if payload.enabled and not model:
            raise HTTPException(
                status_code=400,
                detail="Choose an enabled model before turning smart routing on.",
            )
        if payload.enabled and not await asyncio.to_thread(registry.is_enabled, model):
            raise HTTPException(status_code=400, detail="Routing model is not enabled.")
        updated = settings.model_copy(
            update={"smart_routing_enabled": payload.enabled, "smart_routing_model": model}
        )
        await asyncio.to_thread(save_settings_yaml, updated)
        settings.smart_routing_enabled = payload.enabled
        settings.smart_routing_model = model
        return _smart_routing_payload(settings)


class ChatNamingSettings(BaseModel):
    enabled: bool = False
    model: str = ""


def _chat_naming_payload(settings: AppSettings) -> dict[str, Any]:
    return {
        "enabled": settings.chat_naming_enabled,
        "model": settings.chat_naming_model,
    }


@models_router.get("/api/settings/chat-naming")
async def get_chat_naming(
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    return _chat_naming_payload(settings)


@models_router.put("/api/settings/chat-naming")
async def update_chat_naming(
    payload: ChatNamingSettings,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    registry: ModelRegistry = request.app.state.model_registry
    model = payload.model.strip()
    async with _chat_naming_update_lock:
        if payload.enabled and not model:
            raise HTTPException(
                status_code=400,
                detail="Choose an enabled model before turning chat session naming on.",
            )
        if payload.enabled and not await asyncio.to_thread(registry.is_enabled, model):
            raise HTTPException(status_code=400, detail="Chat naming model is not enabled.")
        updated = settings.model_copy(
            update={"chat_naming_enabled": payload.enabled, "chat_naming_model": model}
        )
        await asyncio.to_thread(save_settings_yaml, updated)
        settings.chat_naming_enabled = payload.enabled
        settings.chat_naming_model = model
        return _chat_naming_payload(settings)


class LocalModelConfigSettings(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    base_url: str = Field(min_length=1, max_length=500)


@models_router.get("/api/settings/models/local/catalog")
async def list_local_model_catalog(
    request: Request,
    base_url: str | None = Query(None, max_length=500),
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    openrouter: OpenRouterClient = request.app.state.openrouter_client
    resolved = _validate_local_base_url(base_url or settings.local_model_base_url)
    models, reachable = await openrouter.list_local_models(resolved)
    return {
        "models": [asdict(m) for m in models],
        "reachable": reachable,
        "base_url": resolved,
    }


@models_router.get("/api/settings/models/local/config")
async def get_local_model_config(
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    return {"base_url": settings.local_model_base_url}


@models_router.put("/api/settings/models/local/config")
async def update_local_model_config(
    payload: LocalModelConfigSettings,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    resolved = _validate_local_base_url(payload.base_url)
    async with _local_config_update_lock:
        updated = settings.model_copy(update={"local_model_base_url": resolved})
        await asyncio.to_thread(save_settings_yaml, updated)
        settings.local_model_base_url = resolved
        return {"base_url": settings.local_model_base_url}


class WebSearchSettings(BaseModel):
    enabled: bool = False


@models_router.get("/api/settings/web-search")
async def get_web_search(
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    return {"enabled": settings.web_search_enabled}


@models_router.put("/api/settings/web-search")
async def update_web_search(
    payload: WebSearchSettings,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    async with _web_search_update_lock:
        updated = settings.model_copy(update={"web_search_enabled": payload.enabled})
        await asyncio.to_thread(save_settings_yaml, updated)
        settings.web_search_enabled = payload.enabled
        return {"enabled": settings.web_search_enabled}
