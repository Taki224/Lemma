from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.session import (
    SessionRecord,
    require_authenticated_session,
    require_csrf,
)
from app.integrity.audit_log import AuditLogStore
from app.integrity.latex_generator import DISCLOSURE_FILENAME, render_disclosure_latex
from app.projects.service import ProjectService

integrity_router = APIRouter()


class CreateAuditEntryRequest(BaseModel):
    source_surface: Literal["chat", "quick_action", "review_studio", "manual"] = "chat"
    provider: Literal["openrouter", "local", "manual"] = "openrouter"
    model_id: str = Field(default="", max_length=200)
    model_display_name: str = Field(default="", max_length=200)
    tier: int = Field(default=2, ge=1, le=3)
    category: str = Field(default="substantive_drafting", max_length=100)
    purpose: str = Field(default="", max_length=500)
    affected_files: list[str] = Field(default_factory=list, max_length=100)
    section_hint: str = Field(default="", max_length=200)
    prompt_summary: str = Field(default="", max_length=1000)
    action_summary: str = Field(default="", max_length=1000)
    outcome: Literal["accepted", "logged_from_chat", "manual"] = "accepted"
    human_verified: bool = False
    verification_note: str = Field(default="", max_length=1000)
    chat_id: str | None = Field(default=None, max_length=100)
    message_id: str | None = Field(default=None, max_length=100)
    hunk_id: str | None = Field(default=None, max_length=100)
    flags: dict[str, bool] = Field(default_factory=dict)


class UpdateAuditEntryRequest(BaseModel):
    purpose: str | None = Field(default=None, max_length=500)
    affected_files: list[str] | None = Field(default=None, max_length=100)
    section_hint: str | None = Field(default=None, max_length=200)
    prompt_summary: str | None = Field(default=None, max_length=1000)
    action_summary: str | None = Field(default=None, max_length=1000)
    human_verified: bool | None = None
    verification_note: str | None = Field(default=None, max_length=1000)


def _get_store(request: Request) -> AuditLogStore:
    store = getattr(request.app.state, "audit_log_store", None)
    if store is None:
        store = AuditLogStore(request.app.state.project_service)
        request.app.state.audit_log_store = store
    return store


def _read_root_tex_content(project_service: ProjectService, project_id: str) -> str:
    meta = project_service.get_project(project_id)
    try:
        file_obj = project_service.read_file(project_id, meta.root_tex)
        return file_obj.text
    except (FileNotFoundError, PermissionError, KeyError, OSError):
        return ""


@integrity_router.get("/api/projects/{project_id}/integrity/log")
async def get_integrity_log(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    store = _get_store(request)
    try:
        log_file = await asyncio.to_thread(store.read_log, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return log_file.to_dict()


@integrity_router.post("/api/projects/{project_id}/integrity/log", status_code=201)
async def create_integrity_log_entry(
    project_id: str,
    body: CreateAuditEntryRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    store = _get_store(request)
    try:
        entry = await asyncio.to_thread(
            store.append_entry,
            project_id,
            source_surface=body.source_surface,
            provider=body.provider,
            model_id=body.model_id,
            model_display_name=body.model_display_name,
            tier=body.tier,
            category=body.category,
            purpose=body.purpose,
            affected_files=body.affected_files,
            section_hint=body.section_hint,
            prompt_summary=body.prompt_summary,
            action_summary=body.action_summary,
            outcome=body.outcome,
            human_verified=body.human_verified,
            verification_note=body.verification_note,
            chat_id=body.chat_id,
            message_id=body.message_id,
            hunk_id=body.hunk_id,
            flags=body.flags,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return entry.to_dict()


@integrity_router.patch("/api/projects/{project_id}/integrity/log/{entry_id}")
async def update_integrity_log_entry(
    project_id: str,
    entry_id: str,
    body: UpdateAuditEntryRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    store = _get_store(request)
    try:
        entry = await asyncio.to_thread(
            store.update_entry,
            project_id,
            entry_id,
            purpose=body.purpose,
            affected_files=body.affected_files,
            section_hint=body.section_hint,
            prompt_summary=body.prompt_summary,
            action_summary=body.action_summary,
            human_verified=body.human_verified,
            verification_note=body.verification_note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return entry.to_dict()


@integrity_router.delete("/api/projects/{project_id}/integrity/log/{entry_id}", status_code=204)
async def delete_integrity_log_entry(
    project_id: str,
    entry_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> None:
    store = _get_store(request)
    try:
        await asyncio.to_thread(store.delete_entry, project_id, entry_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@integrity_router.post("/api/projects/{project_id}/integrity/appendix/preview")
async def preview_integrity_appendix(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    store = _get_store(request)
    project_service: ProjectService = request.app.state.project_service
    try:
        log_file = await asyncio.to_thread(store.read_log, project_id)
        root_tex = await asyncio.to_thread(_read_root_tex_content, project_service, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    latex = render_disclosure_latex(log_file.entries, root_tex_content=root_tex)
    return {
        "filename": DISCLOSURE_FILENAME,
        "latex": latex,
        "entry_count": len(log_file.entries),
    }


@integrity_router.post("/api/projects/{project_id}/integrity/appendix/write")
async def write_integrity_appendix(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Write/update standalone ai_disclosure.tex in the project root without modifying root_tex."""
    store = _get_store(request)
    project_service: ProjectService = request.app.state.project_service
    try:
        log_file = await asyncio.to_thread(store.read_log, project_id)
        root_tex = await asyncio.to_thread(_read_root_tex_content, project_service, project_id)
        latex = render_disclosure_latex(log_file.entries, root_tex_content=root_tex)
        write_res = await asyncio.to_thread(
            project_service.write_file,
            project_id,
            DISCLOSURE_FILENAME,
            latex,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return {
        "path": write_res.path,
        "etag": write_res.etag,
        "size_bytes": write_res.size_bytes,
        "entry_count": len(log_file.entries),
        "latex": latex,
    }
