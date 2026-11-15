from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.router import require_authenticated_session, require_csrf
from app.auth.session import SessionRecord
from app.history.git_snapshots import GitSnapshotService, validate_commit_sha
from app.latex.compiler import CompileOrchestrator
from app.projects.service import ProjectService

history_router = APIRouter(tags=["history"])


class RestoreSnapshotPayload(BaseModel):
    unsaved_buffers: dict[str, str] | None = Field(default=None)


@history_router.get("/api/projects/{project_id}/history")
async def list_project_snapshots(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    git_snapshots: GitSnapshotService = request.app.state.git_snapshots
    try:
        await asyncio.to_thread(project_service.get_project, project_id)
        project_dir = await asyncio.to_thread(project_service.get_project_dir, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    snapshots = await asyncio.to_thread(git_snapshots.list_snapshots, project_dir)
    return {"snapshots": [asdict(s) for s in snapshots]}


@history_router.get("/api/projects/{project_id}/history/{sha}/preview")
async def preview_project_snapshot(
    project_id: str,
    sha: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    try:
        valid_sha = validate_commit_sha(sha)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    project_service: ProjectService = request.app.state.project_service
    git_snapshots: GitSnapshotService = request.app.state.git_snapshots
    try:
        await asyncio.to_thread(project_service.get_project, project_id)
        project_dir = await asyncio.to_thread(project_service.get_project_dir, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    try:
        preview = await asyncio.to_thread(git_snapshots.preview_restore, project_dir, valid_sha)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return asdict(preview)


@history_router.post("/api/projects/{project_id}/history/{sha}/restore")
async def restore_project_snapshot(
    project_id: str,
    sha: str,
    payload: RestoreSnapshotPayload,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    try:
        valid_sha = validate_commit_sha(sha)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    project_service: ProjectService = request.app.state.project_service
    git_snapshots: GitSnapshotService = request.app.state.git_snapshots
    compile_orchestrator: CompileOrchestrator = request.app.state.compile_orchestrator

    try:
        await asyncio.to_thread(project_service.get_project, project_id)
        project_dir = await asyncio.to_thread(project_service.get_project_dir, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    compile_lock = compile_orchestrator.get_lock(project_id)

    try:
        snapshot = await git_snapshots.restore_snapshot(
            project_id=project_id,
            project_dir=project_dir,
            target_sha=valid_sha,
            project_service=project_service,
            compile_lock=compile_lock,
            unsaved_buffers=payload.unsaved_buffers,
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"snapshot": asdict(snapshot)}
