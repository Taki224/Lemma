from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.auth.session import (
    SessionRecord,
    require_authenticated_session,
    require_csrf,
)
from app.config import AppSettings, save_settings_yaml
from app.config_secrets import SecretsStore
from app.projects.service import ETagConflictError, ProjectService
from app.zotero.acceptance import CitationAcceptanceCoordinator
from app.zotero.client import ZoteroClient
from app.zotero.staging_repo import CandidateStagingRepository

zotero_router = APIRouter(tags=["zotero"])


class VerifyZoteroKeyRequest(BaseModel):
    api_key: str | None = None
    library_id: str | None = None
    library_type: Literal["user", "group"] | None = None
    push_enabled: bool | None = None


class SyncZoteroCollectionRequest(BaseModel):
    collection_key: str | None = None
    api_key: str | None = None
    library_id: str | None = None
    library_type: Literal["user", "group"] | None = None


class CandidateDecisionRequest(BaseModel):
    accepted_cite_keys: list[str] = Field(default_factory=list)
    rejected_cite_keys: list[str] = Field(default_factory=list)
    push_enabled: bool | None = None


def _build_zotero_client_from_state(
    request: Request,
    api_key_override: str | None = None,
    library_id_override: str | None = None,
    library_type_override: Literal["user", "group"] | None = None,
) -> ZoteroClient | None:
    secrets_store: SecretsStore = request.app.state.secrets_store
    api_key = (
        api_key_override
        if api_key_override is not None
        else secrets_store.get("zotero_api_key")
    )
    library_id = (
        library_id_override
        if library_id_override is not None
        else secrets_store.get("zotero_library_id")
    )
    raw_lib_type = (
        library_type_override
        if library_type_override is not None
        else secrets_store.get("zotero_library_type") or "user"
    )
    lib_type: Literal["user", "group"] = (
        "group" if str(raw_lib_type).strip().lower() == "group" else "user"
    )
    if not api_key or not library_id:
        return None
    custom_transport: httpx.AsyncBaseTransport | None = getattr(
        request.app.state, "zotero_transport", None
    )
    return ZoteroClient(
        api_key=api_key,
        library_id=library_id,
        library_type=lib_type,
        transport=custom_transport,
    )


@zotero_router.get("/api/projects/{project_id}/zotero")
async def get_zotero_status(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    secrets_store: SecretsStore = request.app.state.secrets_store
    settings: AppSettings = request.app.state.settings

    try:
        meta = await asyncio.to_thread(project_service.get_project, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    api_key = secrets_store.get("zotero_api_key")
    library_id = secrets_store.get("zotero_library_id")
    raw_lib_type = secrets_store.get("zotero_library_type") or "user"
    lib_type: Literal["user", "group"] = (
        "group" if str(raw_lib_type).strip().lower() == "group" else "user"
    )
    return {
        "connected": bool(meta.zotero_collection_key),
        "collection_key": meta.zotero_collection_key,
        "has_credentials": bool(api_key and library_id),
        "library_id": library_id or "",
        "library_type": lib_type,
        "push_enabled": bool(settings.zotero_push_enabled),
    }


@zotero_router.delete("/api/zotero/credentials")
async def clear_zotero_credentials(
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    secrets_store: SecretsStore = request.app.state.secrets_store
    await asyncio.to_thread(
        secrets_store.update_secrets,
        {
            "zotero_api_key": "",
            "zotero_library_id": "",
            "zotero_library_type": "",
        },
    )
    return {"has_credentials": False}


@zotero_router.post("/api/zotero/verify-key")
async def verify_zotero_key(
    payload: VerifyZoteroKeyRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    secrets_store: SecretsStore = request.app.state.secrets_store
    settings: AppSettings = request.app.state.settings

    effective_api_key = (
        payload.api_key.strip()
        if payload.api_key is not None and payload.api_key.strip()
        else (secrets_store.get("zotero_api_key") or "").strip()
    )
    effective_library_id = (
        payload.library_id.strip()
        if payload.library_id is not None and payload.library_id.strip()
        else (secrets_store.get("zotero_library_id") or "").strip()
    )
    raw_lib_type = (
        payload.library_type
        if payload.library_type is not None
        else (secrets_store.get("zotero_library_type") or "user")
    )
    effective_library_type: Literal["user", "group"] = (
        "group" if str(raw_lib_type).strip().lower() == "group" else "user"
    )

    client = _build_zotero_client_from_state(
        request,
        api_key_override=effective_api_key,
        library_id_override=effective_library_id,
        library_type_override=effective_library_type,
    )
    if client is None:
        raise HTTPException(
            status_code=400, detail="Zotero API key and Library ID are required."
        )

    check = await client.verify_key_and_list_collections()
    if check.valid:
        await asyncio.to_thread(
            secrets_store.update_secrets,
            {
                "zotero_api_key": effective_api_key,
                "zotero_library_id": effective_library_id,
                "zotero_library_type": effective_library_type,
            },
        )
        if payload.push_enabled is not None:
            settings.zotero_push_enabled = bool(payload.push_enabled)
            await asyncio.to_thread(save_settings_yaml, settings)

    return asdict(check)


@zotero_router.post("/api/projects/{project_id}/zotero/sync")
async def sync_zotero_collection(
    project_id: str,
    payload: SyncZoteroCollectionRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    settings: AppSettings = request.app.state.settings

    try:
        meta = await asyncio.to_thread(project_service.get_project, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    collection_key = payload.collection_key or meta.zotero_collection_key
    if not collection_key:
        raise HTTPException(
            status_code=400,
            detail="This project is not linked to a Zotero collection. Run the Zotero setup wizard first.",
        )

    client = _build_zotero_client_from_state(
        request,
        api_key_override=payload.api_key,
        library_id_override=payload.library_id,
        library_type_override=payload.library_type,
    )
    if client is None:
        raise HTTPException(
            status_code=400,
            detail="Zotero credentials are not configured. Run the Zotero setup wizard first.",
        )

    try:
        sync_res = await client.sync_collection_to_project(
            collection_key=collection_key,
            project_id=project_id,
            project_service=project_service,
            cache_dir=settings.zotero_cache_dir,
        )
    except ETagConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Zotero sync failed: {exc}"
        ) from exc

    return asdict(sync_res)


@zotero_router.post("/api/projects/{project_id}/zotero/disconnect")
@zotero_router.delete("/api/projects/{project_id}/zotero")
async def disconnect_zotero_collection(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    try:
        meta = await asyncio.to_thread(
            project_service.update_project_settings,
            project_id,
            zotero_collection_key=None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return asdict(meta)


@zotero_router.get("/api/projects/{project_id}/candidates")
async def list_project_candidates(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    staging_repo: CandidateStagingRepository = request.app.state.staging_repo
    try:
        await asyncio.to_thread(project_service.get_project, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    await asyncio.to_thread(staging_repo.expire_stale_candidates)
    candidates = await asyncio.to_thread(
        staging_repo.list_active_candidates, project_id
    )
    return {"candidates": [asdict(c) for c in candidates]}


@zotero_router.post("/api/projects/{project_id}/candidates/decision")
async def decide_project_candidates(
    project_id: str,
    payload: CandidateDecisionRequest,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    coordinator: CitationAcceptanceCoordinator = (
        request.app.state.acceptance_coordinator
    )
    settings: AppSettings = request.app.state.settings

    try:
        meta = await asyncio.to_thread(project_service.get_project, project_id)
        project_dir = await asyncio.to_thread(
            project_service.get_project_dir, project_id
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    push_enabled = (
        bool(payload.push_enabled)
        if payload.push_enabled is not None
        else bool(settings.zotero_push_enabled)
    )
    zotero_client = (
        _build_zotero_client_from_state(request) if push_enabled else None
    )

    try:
        res = await coordinator.accept_hunk_citations(
            project_id=project_id,
            project_dir=project_dir,
            project_service=project_service,
            accepted_cite_keys=payload.accepted_cite_keys,
            rejected_cite_keys=payload.rejected_cite_keys,
            zotero_client=zotero_client,
            collection_key=meta.zotero_collection_key,
            push_enabled=push_enabled,
        )
    except ETagConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return asdict(res)


@zotero_router.post(
    "/api/projects/{project_id}/candidates/{candidate_id}/upload-pdf"
)
async def upload_candidate_pdf(
    project_id: str,
    candidate_id: str,
    request: Request,
    file: UploadFile = File(...),
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    staging_repo: CandidateStagingRepository = request.app.state.staging_repo

    try:
        await asyncio.to_thread(project_service.get_project, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    cand = await asyncio.to_thread(
        staging_repo.get_candidate, project_id, candidate_id
    )
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate source not found")

    MAX_PDF_BYTES = 50 * 1024 * 1024
    chunks: list[bytes] = []
    total_size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAX_PDF_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Uploaded PDF exceeds maximum allowed size ({MAX_PDF_BYTES // (1024 * 1024)}MB).",
            )
        chunks.append(chunk)
    raw_bytes = b"".join(chunks)
    if not raw_bytes or not raw_bytes.startswith(b"%PDF"):
        raise HTTPException(
            status_code=400, detail="Uploaded file must be a valid PDF document"
        )

    orig_name = Path(file.filename or f"{cand.cite_key}.pdf").name
    if not orig_name.lower().endswith(".pdf"):
        orig_name = f"{orig_name}.pdf"
    safe_filename = f"{cand.cite_key}_{orig_name}"

    try:
        write_res = await asyncio.to_thread(
            project_service.save_upload, project_id, safe_filename, raw_bytes
        )
        updated = await asyncio.to_thread(
            staging_repo.attach_pdf, project_id, candidate_id, write_res.path
        )
        indexer = getattr(request.app.state, "pdf_indexer", None)
        if indexer is not None:
            try:
                full_path = project_service.get_project_dir(project_id) / write_res.path
                await asyncio.to_thread(
                    indexer.index_pdf,
                    project_id=project_id,
                    pdf_path=full_path,
                    cite_key=cand.cite_key,
                )
            except Exception:
                logging.getLogger(__name__).warning("Automatic PDF indexing failed for %s", cand.cite_key, exc_info=True)
    except (PermissionError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"candidate": asdict(updated)}
