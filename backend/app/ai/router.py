from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any, Literal

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.ai.chat_service import (
    CHAT_PROFILE_ROLES,
    ChatService,
    InvalidChatProfileError,
)
from app.ai.run_manager import ChatRunManager, RunConflictError
from app.projects.router import MAX_UPLOAD_BYTES
from app.ai.profile_router import (
    AUTO_PROFILE_ID,
    ROUTER_MAX_TOKENS,
    parse_routed_profile,
    route_chat_profile,
    router_messages,
)
from app.config import AppSettings
from app.ai.openrouter import (
    ModelNotAllowedError,
    OpenRouterError,
    PrivacyConsentRequiredError,
    SpendCapExceededError,
)
from app.ai.prompts import PromptsManager, PromptsState
from app.auth.session import (
    SessionRecord,
    require_authenticated_session,
    require_csrf,
)
from app.latex.log_parser import LatexDiagnostic

ai_router = APIRouter(tags=["ai"])
logger = logging.getLogger(__name__)


class UpdatePromptsRequest(BaseModel):
    raw_yaml: str


class CreateChatRequest(BaseModel):
    title: str = "New Chat"


class SendMessageRequest(BaseModel):
    prompt: str
    model: str = "anthropic/claude-3.7-sonnet"
    profile_id: str = "writing"
    active_file: str | None = None
    selection: dict[str, Any] | None = None
    cursor_line: int | None = None
    buffer_text: str | None = None
    # Project-relative paths the user attached; read-only context for this turn.
    references: list[str] = []
    # Unsaved editor text for referenced files, keyed by path.
    reference_buffers: dict[str, str] = {}
    # Files attached to this chat only (stored outside the project), by name.
    attachments: list[str] = []


class DiagnosticInput(BaseModel):
    severity: Literal["error", "warning", "typesetting"] = "error"
    file_path: str = "main.tex"
    line: int | None = None
    message: str
    context_lines: str = ""
    missing_package: str | None = None


class CompileAutofixRequest(BaseModel):
    diagnostic: DiagnosticInput
    model: str = "anthropic/claude-3.7-sonnet"


def _serialize_prompts_state(state: PromptsState, raw_yaml: str) -> dict[str, Any]:
    return {
        "profiles": {pid: asdict(prof) for pid, prof in state.profiles.items()},
        "syntax_warning": state.syntax_warning,
        "error_line": state.error_line,
        "raw_yaml": raw_yaml,
    }


@ai_router.get("/api/ai/prompts")
async def get_ai_prompts(
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    prompts_manager: PromptsManager = request.app.state.prompts_manager
    state = await asyncio.to_thread(prompts_manager.load_or_fallback)
    raw_yaml = await asyncio.to_thread(prompts_manager.get_raw_yaml)
    return _serialize_prompts_state(state, raw_yaml)


@ai_router.put("/api/ai/prompts")
async def update_ai_prompts(
    payload: UpdatePromptsRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    prompts_manager: PromptsManager = request.app.state.prompts_manager
    state = await asyncio.to_thread(prompts_manager.update_raw_yaml, payload.raw_yaml)
    return _serialize_prompts_state(state, payload.raw_yaml)


@ai_router.get("/api/projects/{project_id}/chats")
async def list_project_chats(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    chat_service: ChatService = request.app.state.chat_service
    try:
        chats = await asyncio.to_thread(chat_service.list_chats, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"chats": [asdict(c) for c in chats]}


@ai_router.post("/api/projects/{project_id}/chats", status_code=201)
async def create_project_chat(
    project_id: str,
    payload: CreateChatRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    chat_service: ChatService = request.app.state.chat_service
    try:
        chat = await asyncio.to_thread(
            chat_service.create_chat, project_id, payload.title
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return asdict(chat)


@ai_router.get("/api/projects/{project_id}/chats/{chat_id}/messages")
async def list_chat_messages(
    project_id: str,
    chat_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    chat_service: ChatService = request.app.state.chat_service
    try:
        msgs = await asyncio.to_thread(
            chat_service.list_messages, project_id, chat_id
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"messages": [asdict(m) for m in msgs]}


@ai_router.post("/api/projects/{project_id}/chats/{chat_id}/attachments", status_code=201)
async def upload_chat_attachment(
    project_id: str,
    chat_id: str,
    file: UploadFile,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    chat_service: ChatService = request.app.state.chat_service
    try:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Attachment exceeds maximum allowed size of {MAX_UPLOAD_BYTES} bytes",
            )
        return await asyncio.to_thread(
            chat_service.save_attachment, project_id, chat_id, file.filename or "", content
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    finally:
        await file.close()


@ai_router.delete("/api/projects/{project_id}/chats/{chat_id}/attachments/{name}", status_code=204)
async def delete_chat_attachment(
    project_id: str,
    chat_id: str,
    name: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> None:
    chat_service: ChatService = request.app.state.chat_service
    try:
        await asyncio.to_thread(chat_service.delete_attachment, project_id, chat_id, name)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


async def _load_router_history(
    request: Request,
    chat_service: ChatService,
    profile_id: str,
    project_id: str,
    chat_id: str,
) -> list[dict[str, str]] | None:
    if profile_id.strip().lower() not in {"", AUTO_PROFILE_ID}:
        return None
    settings: AppSettings = request.app.state.settings
    if not (settings.smart_routing_enabled and settings.smart_routing_model.strip()):
        return None
    return await asyncio.to_thread(chat_service._recent_history, project_id, chat_id)


async def _profile_for_prompt(
    request: Request,
    profile_id: str,
    prompt: str,
    session_id: str | None = None,
    has_selection: bool = False,
    history: list[dict[str, str]] | None = None,
) -> str:
    """Honor an explicit profile. Auto uses the smart-routing model when that setting is on."""
    if profile_id.strip().lower() not in {"", AUTO_PROFILE_ID}:
        return profile_id
    settings: AppSettings = request.app.state.settings
    model = settings.smart_routing_model.strip()
    if settings.smart_routing_enabled and model:
        openrouter = request.app.state.openrouter_client
        try:
            completion = await openrouter.complete(
                model=model,
                messages=router_messages(prompt, history=history),
                max_tokens=ROUTER_MAX_TOKENS,
                session_id=session_id,
            )
        except (PrivacyConsentRequiredError, SpendCapExceededError):
            raise
        except (ModelNotAllowedError, OpenRouterError, httpx.HTTPError, json.JSONDecodeError) as exc:
            logger.warning(
                "Smart routing failed (%s); using the keyword profile.",
                type(exc).__name__,
            )
            return route_chat_profile(prompt, has_selection=has_selection)
        chosen = parse_routed_profile(completion.content)
        if chosen is not None:
            return chosen
    return route_chat_profile(prompt, has_selection=has_selection)


@ai_router.post("/api/projects/{project_id}/chats/{chat_id}/messages")
async def send_chat_message(
    project_id: str,
    chat_id: str,
    payload: SendMessageRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    chat_service: ChatService = request.app.state.chat_service
    try:
        await asyncio.to_thread(chat_service.project_service.get_project, project_id)
        reply_model = payload.model.strip()
        if reply_model and not await asyncio.to_thread(
            request.app.state.model_registry.is_enabled, reply_model
        ):
            raise ModelNotAllowedError(
                f"Model {reply_model!r} is not enabled. Enable it in Settings > Models."
            )
        has_selection = isinstance(payload.selection, dict) and (
            bool(payload.selection.get("text")) or bool(payload.selection.get("start_line"))
        )
        router_history = await _load_router_history(
            request, chat_service, payload.profile_id, project_id, chat_id
        )
        profile_id = await _profile_for_prompt(
            request,
            payload.profile_id,
            payload.prompt,
            session_id=f"{project_id}:{chat_id}"[:256],
            has_selection=has_selection,
            history=router_history,
        )
        turn = await chat_service.send_message(
            project_id=project_id,
            chat_id=chat_id,
            user_prompt=payload.prompt,
            model=payload.model,
            profile_id=profile_id,
            active_file=payload.active_file,
            selection=payload.selection,
            cursor_line=payload.cursor_line,
            buffer_text=payload.buffer_text,
            references=payload.references,
            reference_buffers=payload.reference_buffers,
            attachments=payload.attachments,
        )
    except InvalidChatProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PrivacyConsentRequiredError, ModelNotAllowedError, PermissionError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SpendCapExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except OpenRouterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return asdict(turn)


@ai_router.post("/api/projects/{project_id}/chats/{chat_id}/runs", status_code=201)
async def start_chat_run(
    project_id: str,
    chat_id: str,
    payload: SendMessageRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    chat_service: ChatService = request.app.state.chat_service
    chat_run_manager: ChatRunManager = request.app.state.chat_run_manager
    try:
        await asyncio.to_thread(chat_service.project_service.get_project, project_id)
        reply_model = payload.model.strip()
        if reply_model and not await asyncio.to_thread(
            request.app.state.model_registry.is_enabled, reply_model
        ):
            raise ModelNotAllowedError(
                f"Model {reply_model!r} is not enabled. Enable it in Settings > Models."
            )
        if chat_run_manager.get_active_run() is not None:
            raise HTTPException(status_code=409, detail="Another chat run is already active")
        has_selection = isinstance(payload.selection, dict) and (
            bool(payload.selection.get("text")) or bool(payload.selection.get("start_line"))
        )
        router_history = await _load_router_history(
            request, chat_service, payload.profile_id, project_id, chat_id
        )
        profile_id = await _profile_for_prompt(
            request,
            payload.profile_id,
            payload.prompt,
            session_id=f"{project_id}:{chat_id}"[:256],
            has_selection=has_selection,
            history=router_history,
        )
        profile = await asyncio.to_thread(
            chat_service.prompts_manager.get_profile, profile_id
        )
        if profile.role not in CHAT_PROFILE_ROLES:
            raise InvalidChatProfileError(f"Profile '{profile_id}' is not a chat profile.")
        state = chat_run_manager.start_run(
            chat_service,
            project_id=project_id,
            chat_id=chat_id,
            user_prompt=payload.prompt,
            model=payload.model,
            profile_id=profile_id,
            active_file=payload.active_file,
            selection=payload.selection,
            cursor_line=payload.cursor_line,
            buffer_text=payload.buffer_text,
            references=payload.references,
            reference_buffers=payload.reference_buffers,
            attachments=payload.attachments,
        )
    except InvalidChatProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PrivacyConsentRequiredError, ModelNotAllowedError, PermissionError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SpendCapExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "run_id": state.run_id,
        "project_id": project_id,
        "chat_id": chat_id,
        "profile_id": profile_id,
        "status": state.status,
    }


@ai_router.get("/api/ai/runs/active")
async def get_active_chat_run(
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    chat_run_manager: ChatRunManager = request.app.state.chat_run_manager
    state = chat_run_manager.get_active_run()
    if state is None:
        return {"run": None}
    return {
        "run": {
            "run_id": state.run_id,
            "project_id": state.project_id,
            "chat_id": state.chat_id,
            "profile_id": state.profile_id,
            "status": state.status,
            "events": list(state.events),
        }
    }


@ai_router.get("/api/ai/runs/{run_id}/events")
async def stream_chat_run_events(
    run_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> StreamingResponse:
    chat_run_manager: ChatRunManager = request.app.state.chat_run_manager
    if chat_run_manager.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown run {run_id!r}")

    async def event_stream():
        async for evt in chat_run_manager.subscribe(run_id):
            yield f"event: {evt['type']}\ndata: {json.dumps(evt)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@ai_router.post("/api/ai/runs/{run_id}/stop")
async def stop_chat_run(
    run_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    chat_run_manager: ChatRunManager = request.app.state.chat_run_manager
    try:
        state = await chat_run_manager.stop_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"run_id": state.run_id, "status": state.status}


@ai_router.post("/api/projects/{project_id}/compile/autofix")
async def propose_compile_autofix(
    project_id: str,
    payload: CompileAutofixRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    chat_service: ChatService = request.app.state.chat_service
    diag = LatexDiagnostic(
        severity=payload.diagnostic.severity,
        file_path=payload.diagnostic.file_path,
        line=payload.diagnostic.line,
        message=payload.diagnostic.message,
        context_lines=payload.diagnostic.context_lines,
        missing_package=payload.diagnostic.missing_package,
    )
    try:
        hunks = await chat_service.propose_compile_fix(
            project_id=project_id,
            diagnostic=diag,
            model=payload.model,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PrivacyConsentRequiredError, ModelNotAllowedError, PermissionError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SpendCapExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except OpenRouterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"hunks": [asdict(h) for h in hunks]}
