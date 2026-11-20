from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.auth.session import (
    SessionRecord,
    require_authenticated_session,
    require_csrf,
)
from app.config import AppSettings
from app.latex.compiler import CompileOrchestrator, detect_root_tex
from app.latex.ctan_helper import InvalidPackageNameError, save_manual_sty_upload
from app.latex.log_parser import parse_bibliography_log, parse_latex_log
from app.latex.synctex import SyncTexService
from app.projects.service import ProjectService

MAX_TEXMF_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

latex_router = APIRouter()


def _resolve_project_and_build(
    request: Request,
    project_id: str,
) -> tuple[ProjectService, Path, str, Path]:
    service: ProjectService = request.app.state.project_service
    try:
        meta = service.get_project(project_id)
        project_dir = service.get_project_dir(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    root_tex = detect_root_tex(project_dir, configured_root=meta.root_tex)
    build_dir = project_dir / ".build"
    return service, project_dir, root_tex, build_dir


@latex_router.post("/api/projects/{project_id}/compile")
async def compile_project_endpoint(
    project_id: str,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service, project_dir, root_tex, build_dir = _resolve_project_and_build(request, project_id)
    meta = service.get_project(project_id)
    orchestrator: CompileOrchestrator = request.app.state.compile_orchestrator

    report = await orchestrator.compile_project(
        project_id=project_id,
        project_dir=project_dir,
        root_tex=root_tex,
        **({"tex_engine": meta.tex_engine} if meta.tex_engine != "pdflatex" else {}),
    )

    log_text = ""
    if report.log_path and report.log_path.is_file():
        log_text = await asyncio.to_thread(
            report.log_path.read_text, "utf-8", "replace"
        )
    else:
        log_text = report.log_excerpt

    diagnostics = parse_latex_log(log_text=log_text, root_tex=report.root_tex)
    blg_path = build_dir / f"{Path(report.root_tex).stem}.blg"
    if report.status == "failed" and blg_path.is_file():
        blg_text = await asyncio.to_thread(blg_path.read_text, "utf-8", "replace")
        diagnostics += parse_bibliography_log(blg_text=blg_text, root_tex=report.root_tex)
    pdf_url = (
        f"/api/projects/{project_id}/pdf"
        if report.pdf_path and report.pdf_path.is_file()
        else None
    )

    return {
        "status": report.status,
        "duration_ms": report.duration_ms,
        "diagnostics": [asdict(d) for d in diagnostics],
        "pdf_url": pdf_url,
        "missing_packages": report.missing_packages,
        "offline_hint": report.offline_hint,
    }


@latex_router.get("/api/projects/{project_id}/pdf")
async def get_compiled_pdf(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> FileResponse:
    _, _, root_tex, build_dir = _resolve_project_and_build(request, project_id)
    stem = Path(root_tex).stem
    candidate_pdfs = [
        build_dir / f"{stem}.pdf",
        build_dir / Path(root_tex).with_suffix(".pdf"),
    ]
    pdf_path = next((p for p in candidate_pdfs if p.is_file()), None)
    if pdf_path is None:
        raise HTTPException(status_code=404, detail="Compiled PDF not found; compile the project first.")

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"{project_id}.pdf",
        headers={"Cache-Control": "no-store"},
    )


@latex_router.get("/api/projects/{project_id}/synctex/forward")
async def synctex_forward_endpoint(
    project_id: str,
    request: Request,
    file: str = Query(..., description="Project-relative .tex file path"),
    line: int = Query(..., ge=1, description="1-based line number"),
    column: int = Query(1, ge=1, description="1-based column number"),
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    service, project_dir, root_tex, build_dir = _resolve_project_and_build(request, project_id)
    try:
        validated_target = service._validate_project_rel_path(project_dir, file)
        norm_file = validated_target.relative_to(project_dir.resolve()).as_posix()
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    stem = Path(root_tex).stem
    pdf_name = f"{stem}.pdf"
    if not (build_dir / pdf_name).is_file():
        alt_pdf = build_dir / Path(root_tex).with_suffix(".pdf")
        if alt_pdf.is_file():
            pdf_name = alt_pdf.name
        else:
            raise HTTPException(status_code=404, detail="Compiled PDF not found for SyncTeX forward lookup.")

    synctex_service: SyncTexService = getattr(
        request.app.state, "synctex_service", None
    ) or SyncTexService()
    res = await asyncio.to_thread(
        synctex_service.forward_sync,
        build_dir,
        pdf_name,
        norm_file,
        line,
        column,
    )
    return asdict(res)


@latex_router.get("/api/projects/{project_id}/synctex/inverse")
async def synctex_inverse_endpoint(
    project_id: str,
    request: Request,
    page: int = Query(..., ge=1, description="1-based PDF page number"),
    x: float = Query(..., ge=0.0, description="Horizontal coordinate in PDF points"),
    y: float = Query(..., ge=0.0, description="Vertical coordinate in PDF points"),
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    import math

    if not math.isfinite(x) or not math.isfinite(y):
        raise HTTPException(status_code=400, detail="Coordinates x and y must be finite numbers")

    _, _, root_tex, build_dir = _resolve_project_and_build(request, project_id)
    stem = Path(root_tex).stem
    pdf_name = f"{stem}.pdf"
    if not (build_dir / pdf_name).is_file():
        alt_pdf = build_dir / Path(root_tex).with_suffix(".pdf")
        if alt_pdf.is_file():
            pdf_name = alt_pdf.name
        else:
            raise HTTPException(status_code=404, detail="Compiled PDF not found for SyncTeX inverse lookup.")

    synctex_service: SyncTexService = getattr(
        request.app.state, "synctex_service", None
    ) or SyncTexService()
    res = await asyncio.to_thread(
        synctex_service.inverse_sync,
        build_dir,
        pdf_name,
        page,
        x,
        y,
    )
    return asdict(res)


@latex_router.post("/api/projects/{project_id}/texmf/upload", status_code=201)
async def upload_project_texmf_file(
    project_id: str,
    file: UploadFile,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, str]:
    _, project_dir, _, _ = _resolve_project_and_build(request, project_id)
    settings: AppSettings = request.app.state.settings
    try:
        chunks: list[bytes] = []
        total_size = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > MAX_TEXMF_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Package file exceeds maximum size of {MAX_TEXMF_UPLOAD_BYTES} bytes",
                )
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)
        filename = (file.filename or "").strip()
        try:
            saved_path = await asyncio.to_thread(
                save_manual_sty_upload,
                filename=filename,
                content=raw_bytes,
                project_dir=project_dir,
                texmf_dir=settings.texmf_dir,
            )
        except InvalidPackageNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {
            "filename": saved_path.name,
            "path": saved_path.relative_to(project_dir.resolve()).as_posix(),
        }
    finally:
        await file.close()
