from __future__ import annotations

import asyncio
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.ai.openrouter import SpendCapExceededError
from app.auth.router import require_authenticated_session, require_csrf
from app.auth.session import SessionRecord
from app.config import AppSettings
from app.history.git_snapshots import GitSnapshotService
from app.projects.service import ProjectService
from app.review_studio.report_store import ReviewReportStore
from app.review_studio.text_review import (
    ConfirmationRequiredError,
    ReviewerAssignment,
    TextReviewOrchestrator,
)
from app.review_studio.visual_inspector import VisualPageInspector

review_studio_router = APIRouter(tags=["review_studio"])


class ReviewerAssignmentPayload(BaseModel):
    persona_id: str
    model: str


class ReviewEstimatePayload(BaseModel):
    assignments: list[ReviewerAssignmentPayload] = Field(default_factory=list)
    include_visual: bool = Field(default=False)
    vision_model: str | None = Field(default=None)


class ReviewRunPayload(BaseModel):
    assignments: list[ReviewerAssignmentPayload] = Field(default_factory=list)
    include_visual: bool = Field(default=False)
    vision_model: str | None = Field(default=None)
    confirm_high_cost: bool = Field(default=False)


@review_studio_router.post("/api/projects/{project_id}/reviews/estimate")
async def estimate_project_review(
    project_id: str,
    payload: ReviewEstimatePayload,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    orchestrator: TextReviewOrchestrator = request.app.state.text_review_orchestrator
    settings: AppSettings = request.app.state.settings

    try:
        project = await asyncio.to_thread(project_service.get_project, project_id)
        project_dir = await asyncio.to_thread(project_service.get_project_dir, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    root_tex = getattr(project, "root_tex", None) or "main.tex"
    assignments = [
        ReviewerAssignment(persona_id=a.persona_id, model=a.model)
        for a in payload.assignments
    ]

    try:
        estimate = orchestrator.estimate_review_run(
            project_dir=project_dir,
            root_tex=root_tex,
            assignments=assignments,
            include_visual=payload.include_visual,
            settings=settings,
            vision_model=payload.vision_model,
        )
    except SpendCapExceededError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return asdict(estimate)


@review_studio_router.post("/api/projects/{project_id}/reviews/run")
async def run_project_review(
    project_id: str,
    payload: ReviewRunPayload,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    orchestrator: TextReviewOrchestrator = request.app.state.text_review_orchestrator
    visual_inspector: VisualPageInspector = request.app.state.visual_inspector
    report_store: ReviewReportStore = request.app.state.review_report_store
    git_snapshots: GitSnapshotService = request.app.state.git_snapshots
    settings: AppSettings = request.app.state.settings

    try:
        project = await asyncio.to_thread(project_service.get_project, project_id)
        project_dir = await asyncio.to_thread(project_service.get_project_dir, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    root_tex = getattr(project, "root_tex", None) or "main.tex"
    assignments = [
        ReviewerAssignment(persona_id=a.persona_id, model=a.model)
        for a in payload.assignments
    ]

    model_registry = request.app.state.model_registry
    required_models = [a.model for a in payload.assignments]
    if payload.include_visual and payload.vision_model:
        required_models.append(payload.vision_model)
    for required_model in required_models:
        if not model_registry.is_enabled(required_model):
            raise HTTPException(
                status_code=403,
                detail=f"Model {required_model!r} is not enabled. Enable it in Settings > Models.",
            )

    # Create / record Git snapshot SHA before review run to pin the reviewed state
    snapshot_info = await asyncio.to_thread(
        git_snapshots.create_snapshot,
        project_dir=project_dir,
        trigger="manual",
        message="Review run snapshot",
    )
    snapshot_sha = snapshot_info.commit_sha if snapshot_info else "unknown"
    review_session_id = f"review:{project_id}:{secrets.token_hex(8)}"[:256]

    try:
        text_res = await orchestrator.run_parallel_reviewers(
            project_dir=project_dir,
            root_tex=root_tex,
            assignments=assignments,
            confirm_high_cost=payload.confirm_high_cost,
            settings=settings,
            session_id=review_session_id,
            academic_integrity_mode=bool(getattr(project, "academic_integrity_mode", False)),
        )
    except ConfirmationRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SpendCapExceededError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    all_issues = list(text_res.issues)

    if payload.include_visual and payload.vision_model:
        build_dir = (project_dir / ".build").resolve()
        root_name = Path(root_tex).name
        root_stem = root_name[:-4] if root_name.endswith(".tex") else Path(root_name).stem
        candidates = [
            build_dir / f"{root_stem}.pdf",
            build_dir / "main.pdf",
        ]
        target_pdf: Path | None = None
        for cand in candidates:
            if cand.resolve().is_relative_to(build_dir) and cand.is_file():
                target_pdf = cand
                break

        if target_pdf:
            try:
                vis_issues = await visual_inspector.inspect_pages(
                    pdf_path=target_pdf,
                    vision_model=payload.vision_model,
                    openrouter=request.app.state.openrouter_client,
                    session_id=review_session_id,
                )
                all_issues.extend(vis_issues)
            except Exception:
                pass

    report_path = report_store.save_report(
        project_dir=project_dir,
        snapshot_sha=snapshot_sha,
        issues=all_issues,
    )

    return {
        "report_id": report_path.stem,
        "snapshot_sha": snapshot_sha,
        "issues": [asdict(i) for i in all_issues],
        "failed_reviewers": text_res.failed_reviewers,
    }


@review_studio_router.get("/api/projects/{project_id}/reviews")
async def list_project_reviews(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    report_store: ReviewReportStore = request.app.state.review_report_store

    try:
        await asyncio.to_thread(project_service.get_project, project_id)
        project_dir = await asyncio.to_thread(project_service.get_project_dir, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    reports = report_store.list_reports(project_dir)
    return {"reports": reports}


@review_studio_router.get("/api/projects/{project_id}/reviews/{report_id}")
async def get_project_review(
    project_id: str,
    report_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    if not report_id or "/" in report_id or "\\" in report_id or ".." in report_id:
        raise HTTPException(status_code=400, detail="Invalid report_id format")

    project_service: ProjectService = request.app.state.project_service
    report_store: ReviewReportStore = request.app.state.review_report_store

    try:
        await asyncio.to_thread(project_service.get_project, project_id)
        project_dir = await asyncio.to_thread(project_service.get_project_dir, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    resolved_issues = report_store.resolve_issue_locations(project_dir, report_id)
    if not resolved_issues:
        reviews_dir = (project_dir / "paper-reviews").resolve()
        candidates = [
            reviews_dir / f"{report_id}.md",
            reviews_dir / report_id,
        ]
        if not any(c.resolve().is_relative_to(reviews_dir) and c.is_file() for c in candidates):
            raise HTTPException(status_code=404, detail=f"Review report '{report_id}' not found")

    return {
        "report_id": report_id,
        "issues": [asdict(i) for i in resolved_issues],
    }
