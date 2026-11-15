from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.ai.openrouter import ModelNotAllowedError
from app.auth.router import require_authenticated_session, require_csrf
from app.auth.session import SessionRecord
from app.grounding.citation_verifier import BibEntry, parse_bib_file
from app.projects.service import ProjectService
from app.tools.exporter import export_camera_ready_zip
from app.tools.health_check import run_citation_health_check
from app.tools.quick_actions import run_selection_quick_action
from app.tools.visual_to_latex import (
    convert_csv_to_booktabs,
    convert_image_to_latex,
    lint_generated_latex_snippet,
)

logger = logging.getLogger(__name__)

tools_router = APIRouter(tags=["tools"])


class HealthCheckPayload(BaseModel):
    root_tex: str | None = None


class VisualToLatexPayload(BaseModel):
    csv_text: str | None = None
    caption: str = ""
    label: str = "tab:results"
    image_base64: str | None = None
    target_format: Literal["equation", "booktabs", "tikz"] = "equation"
    model: str = "anthropic/claude-3.7-sonnet"
    file_path: str = "main.tex"
    base_etag: str = ""
    start_line: int = 1
    end_line: int = 1


class QuickActionPayload(BaseModel):
    file_path: str
    start_line: int = 1
    end_line: int = 1
    selected_text: str
    action: Literal["tighten", "verify_claim", "formal_tone", "explain_math"]
    model: str = "anthropic/claude-3.7-sonnet"


class ExportPayload(BaseModel):
    root_tex: str | None = None


def _get_project_context(request: Request, project_id: str) -> tuple[Any, Path]:
    project_service: ProjectService = request.app.state.project_service
    try:
        project = project_service.get_project(project_id)
        project_dir = project_service.get_project_dir(project_id)
        return project, project_dir
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _load_bib_and_staged_keys(project_dir: Path, db: Any, project_id: str) -> tuple[dict[str, BibEntry], set[str]]:
    bib_entries: dict[str, BibEntry] = {}
    for bib_path in project_dir.glob("*.bib"):
        try:
            entries = parse_bib_file(bib_path.read_text(encoding="utf-8", errors="replace"))
            bib_entries.update(entries)
        except Exception as e:
            logger.warning("Error parsing bib file %s: %s", bib_path, e)

    staged_keys: set[str] = set()
    if db is not None:
        try:
            with contextlib.closing(db.get_connection()) as conn:
                rows = conn.execute(
                    "SELECT cite_key FROM candidate_sources WHERE project_id = ? AND status IN ('staged', 'accepted')",
                    (project_id,),
                ).fetchall()
                for r in rows:
                    if r["cite_key"]:
                        staged_keys.add(r["cite_key"])
        except Exception as e:
            logger.warning("Error loading staged keys for %s: %s", project_id, e)

    return bib_entries, staged_keys


@tools_router.post("/api/projects/{project_id}/tools/health-check")
async def citation_health_check(
    project_id: str,
    payload: HealthCheckPayload,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    project, project_dir = _get_project_context(request, project_id)
    root_tex = payload.root_tex or getattr(project, "root_tex", None) or "main.tex"
    db = getattr(request.app.state, "db", None)

    try:
        report = await asyncio.to_thread(
            run_citation_health_check,
            project_dir=project_dir,
            root_tex=root_tex,
            db=db,
            project_id=project_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return asdict(report)


@tools_router.post("/api/projects/{project_id}/tools/visual-to-latex")
async def visual_to_latex(
    project_id: str,
    payload: VisualToLatexPayload,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    project, project_dir = _get_project_context(request, project_id)
    db = getattr(request.app.state, "db", None)
    openrouter = getattr(request.app.state, "openrouter_client", None)
    bib_entries, staged_keys = await asyncio.to_thread(_load_bib_and_staged_keys, project_dir, db, project_id)

    if payload.csv_text is not None:
        table_code = convert_csv_to_booktabs(
            csv_text=payload.csv_text,
            caption=payload.caption,
            label=payload.label,
        )
        res = lint_generated_latex_snippet(
            latex_code=table_code,
            bib_entries=bib_entries,
            staged_keys=staged_keys,
            target_format="booktabs",
            file_path=payload.file_path,
            base_etag=payload.base_etag,
            start_line=payload.start_line,
            end_line=payload.end_line,
        )
        data = asdict(res)
        if res.hunk:
            data["hunk"] = asdict(res.hunk)
        return data

    elif payload.image_base64 is not None:
        if not openrouter:
            raise HTTPException(status_code=500, detail="OpenRouter client not available.")
        try:
            image_bytes = base64.b64decode(payload.image_base64)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 image data: {e}")

        try:
            res = await convert_image_to_latex(
                image_bytes=image_bytes,
                target_format=payload.target_format,
                model=payload.model,
                openrouter=openrouter,
                bib_entries=bib_entries,
                staged_keys=staged_keys,
                file_path=payload.file_path,
                base_etag=payload.base_etag,
            )
        except ModelNotAllowedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        data = asdict(res)
        if res.hunk:
            data["hunk"] = asdict(res.hunk)
        return data

    raise HTTPException(status_code=400, detail="Either csv_text or image_base64 must be provided.")


@tools_router.post("/api/projects/{project_id}/tools/quick-action")
async def selection_quick_action(
    project_id: str,
    payload: QuickActionPayload,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    project, project_dir = _get_project_context(request, project_id)
    db = getattr(request.app.state, "db", None)
    settings = getattr(request.app.state, "settings", None)
    openrouter = getattr(request.app.state, "openrouter_client", None)
    bib_entries, staged_keys = await asyncio.to_thread(_load_bib_and_staged_keys, project_dir, db, project_id)

    try:
        res = await run_selection_quick_action(
            project_id=project_id,
            project_dir=project_dir,
            file_path=payload.file_path,
            start_line=payload.start_line,
            end_line=payload.end_line,
            selected_text=payload.selected_text,
            action=payload.action,
            model=payload.model,
            openrouter=openrouter,
            db=db,
            settings=settings,
            bib_entries=bib_entries,
            staged_keys=staged_keys,
            prompts_manager=getattr(request.app.state, "prompts_manager", None),
        )
    except ModelNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    data = asdict(res)
    if res.hunk:
        data["hunk"] = asdict(res.hunk)
    return data


@tools_router.post("/api/projects/{project_id}/export")
async def export_project(
    project_id: str,
    payload: ExportPayload,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> FileResponse:
    project, project_dir = _get_project_context(request, project_id)
    compiler = getattr(request.app.state, "compile_orchestrator", None)
    settings = getattr(request.app.state, "settings", None)

    if not compiler or not settings:
        raise HTTPException(status_code=500, detail="Compiler or settings not initialized.")

    root_tex = payload.root_tex or getattr(project, "root_tex", None) or "main.tex"

    try:
        result = await export_camera_ready_zip(
            project_id=project_id,
            project_dir=project_dir,
            compiler=compiler,
            settings=settings,
            root_tex=root_tex,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    headers: dict[str, str] = {
        "x-verification-passed": "true" if result.verification_compile_passed else "false",
        "x-included-count": str(len(result.included_files)),
        "x-stripped-count": str(len(result.stripped_files)),
    }
    # Safely attach file lists to headers only if ASCII-safe and within 2000 chars each
    try:
        inc_json = json.dumps(result.included_files)
        strip_json = json.dumps(result.stripped_files)
        if len(inc_json) < 2000 and len(strip_json) < 2000:
            headers["x-included-files"] = inc_json
            headers["x-stripped-files"] = strip_json
    except Exception:
        pass

    return FileResponse(
        path=str(result.zip_path),
        filename=f"{project_id}_camera_ready.zip",
        media_type="application/zip",
        headers=headers,
    )
