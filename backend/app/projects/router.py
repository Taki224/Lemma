from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from starlette.datastructures import Headers

from app.auth.session import (
    SessionRecord,
    authenticate_websocket,
    require_authenticated_session,
    require_csrf,
)
from app.latex.outline import OutlineNode, parse_project_outline
from app.projects.service import ETagConflictError, ProjectService, _UNSET

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB max per upload

projects_router = APIRouter()


class CreateProjectRequest(BaseModel):
    name: str
    template: str = "blank"


class UpdateProjectSettingsRequest(BaseModel):
    root_tex: str | None = None
    zotero_collection_key: str | None = None
    tex_engine: str | None = None


class RenameProjectRequest(BaseModel):
    name: str


class DuplicateProjectRequest(BaseModel):
    name: str | None = None


class CreateFolderRequest(BaseModel):
    path: str


class MovePathRequest(BaseModel):
    from_path: str
    to_path: str


class WriteFileRequest(BaseModel):
    content: str
    expected_etag: str | None = None


def _get_project_service(request: Request) -> ProjectService:
    return request.app.state.project_service


def _get_project_index_store(request: Request) -> Any:
    return getattr(request.app.state, "project_index_store", None)


def _serialize_outline(nodes: list[OutlineNode]) -> list[dict[str, Any]]:
    return [asdict(n) for n in nodes]


@projects_router.get("/api/projects")
async def list_projects(
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    service = _get_project_service(request)
    projects = service.list_projects()
    return {"projects": [asdict(p) for p in projects]}


@projects_router.post("/api/projects", status_code=201)
async def create_project(
    body: CreateProjectRequest,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _get_project_service(request)
    try:
        meta = service.create_project(name=body.name, template=body.template)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(meta)


@projects_router.patch("/api/projects/{project_id}/settings")
async def update_project_settings(
    project_id: str,
    body: UpdateProjectSettingsRequest,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _get_project_service(request)
    zotero_arg = (
        body.zotero_collection_key
        if "zotero_collection_key" in body.model_fields_set
        else _UNSET
    )
    try:
        meta = service.update_project_settings(
            project_id=project_id,
            root_tex=body.root_tex,
            zotero_collection_key=zotero_arg,
            tex_engine=body.tex_engine,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(meta)


@projects_router.patch("/api/projects/{project_id}")
async def rename_project(
    project_id: str,
    body: RenameProjectRequest,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _get_project_service(request)
    try:
        meta = await asyncio.to_thread(service.rename_project, project_id, body.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(meta)


@projects_router.post("/api/projects/{project_id}/duplicate", status_code=201)
async def duplicate_project(
    project_id: str,
    body: DuplicateProjectRequest,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _get_project_service(request)
    try:
        meta = await asyncio.to_thread(service.duplicate_project, project_id, body.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return asdict(meta)


@projects_router.delete("/api/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> None:
    service = _get_project_service(request)
    try:
        await asyncio.to_thread(service.delete_project, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@projects_router.get("/api/projects/{project_id}/files")
async def list_project_files(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    service = _get_project_service(request)
    try:
        files = service.list_files(project_id)
        folders = service.list_folders(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"files": [asdict(f) for f in files], "folders": folders}


@projects_router.delete("/api/projects/{project_id}/files/{file_path:path}", status_code=204)
async def delete_project_file(
    project_id: str,
    file_path: str,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> None:
    service = _get_project_service(request)
    try:
        await asyncio.to_thread(service.delete_file, project_id, file_path)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    index_store = _get_project_index_store(request)
    if index_store is not None:
        await asyncio.to_thread(index_store.delete_file, project_id, file_path)


@projects_router.post("/api/projects/{project_id}/folders", status_code=201)
async def create_project_folder(
    project_id: str,
    body: CreateFolderRequest,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _get_project_service(request)
    try:
        path = await asyncio.to_thread(service.create_folder, project_id, body.path)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"path": path}


@projects_router.delete("/api/projects/{project_id}/folders/{folder_path:path}", status_code=204)
async def delete_project_folder(
    project_id: str,
    folder_path: str,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> None:
    service = _get_project_service(request)
    try:
        await asyncio.to_thread(service.delete_folder, project_id, folder_path)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    index_store = _get_project_index_store(request)
    if index_store is not None:
        await asyncio.to_thread(index_store.delete_file, project_id, folder_path)


@projects_router.post("/api/projects/{project_id}/move")
async def move_project_path(
    project_id: str,
    body: MovePathRequest,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _get_project_service(request)
    try:
        path, meta = await asyncio.to_thread(service.move_path, project_id, body.from_path, body.to_path)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    index_store = _get_project_index_store(request)
    if index_store is not None:
        await asyncio.to_thread(index_store.move_path, project_id, body.from_path, path)
    return {"path": path, "project": asdict(meta)}


@projects_router.get("/api/projects/{project_id}/files/{file_path:path}")
async def read_project_file(
    project_id: str,
    file_path: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    service = _get_project_service(request)
    try:
        result = service.read_file(project_id, file_path)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "path": result.path,
        "content": result.text,
        "etag": result.etag,
        "is_binary": result.is_binary,
    }


@projects_router.put("/api/projects/{project_id}/files/{file_path:path}")
async def write_project_file(
    project_id: str,
    file_path: str,
    body: WriteFileRequest,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> Any:
    service = _get_project_service(request)
    try:
        meta = service.get_project(project_id)
        if file_path.strip("/") == "references.bib" and meta.zotero_collection_key:
            raise HTTPException(
                status_code=403,
                detail="This file is Zotero synced and manual editing is locked. Disconnect Zotero to edit it manually.",
            )
        result = service.write_file(
            project_id=project_id,
            rel_path=file_path,
            content=body.content.encode("utf-8"),
            expected_etag=body.expected_etag,
        )
    except ETagConflictError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "ETag conflict: file modified on disk",
                "current_etag": exc.current_etag,
                "server_content": exc.server_content,
            },
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PermissionError, IsADirectoryError, FileExistsError) as exc:
        raise HTTPException(status_code=400 if isinstance(exc, (IsADirectoryError, FileExistsError)) else 403, detail=str(exc)) from exc

    index_store = _get_project_index_store(request)
    if index_store is not None:
        await asyncio.to_thread(index_store.upsert_file, project_id, result.path, body.content)

    return asdict(result)


@projects_router.post("/api/projects/{project_id}/uploads", status_code=201)
async def upload_project_file(
    project_id: str,
    file: UploadFile,
    request: Request,
    folder: str = Form(""),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _get_project_service(request)
    try:
        # Stream read with size ceiling to prevent OOM
        chunks: list[bytes] = []
        total_size = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload exceeds maximum allowed size of {MAX_UPLOAD_BYTES} bytes",
                )
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)

        try:
            result = service.save_upload(
                project_id=project_id,
                filename=file.filename or "",
                content=raw_bytes,
                folder=folder,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        index_store = _get_project_index_store(request)
        if index_store is not None:
            await asyncio.to_thread(index_store.upsert_file, project_id, result.path)
        return asdict(result)
    finally:
        await file.close()


@projects_router.get("/api/projects/{project_id}/outline")
async def get_project_outline(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    service = _get_project_service(request)
    try:
        meta = service.get_project(project_id)
        project_dir = service.get_project_dir(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    nodes = parse_project_outline(project_dir=project_dir, root_tex=meta.root_tex)
    return {"outline": _serialize_outline(nodes)}


@projects_router.websocket("/api/projects/{project_id}/ws")
async def project_websocket(websocket: WebSocket, project_id: str) -> None:
    # CSWSH check: validate Origin matches Host if Origin is present
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if origin and host:
        origin_host = origin.split("://")[-1].split("/")[0]
        if origin_host.lower() != host.lower():
            await websocket.close(code=4403)
            return

    if not await authenticate_websocket(websocket):
        await websocket.close(code=4401)
        return

    service: ProjectService = websocket.app.state.project_service
    try:
        service.get_project(project_id)
    except (KeyError, PermissionError):
        await websocket.close(code=4404)
        return

    await websocket.accept()
    await websocket.send_json({"type": "connected", "project_id": project_id})

    async def _watch_task() -> None:
        try:
            async for ev in service.watch_project_files(project_id):
                await websocket.send_json(
                    {
                        "type": "file_changed",
                        "change_type": ev.change_type,
                        "path": ev.path,
                        "etag": ev.etag,
                    }
                )
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass

    watcher = asyncio.create_task(_watch_task())
    try:
        while True:
            try:
                msg = await websocket.receive_json()
            except ValueError:
                # Malformed JSON frame
                continue
            if isinstance(msg, dict) and msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
