from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.ai.chat_service import ChatService, ProposedHunk
from app.ai.model_registry import ModelRegistry, build_seed_models
from app.ai.openrouter import DEFAULT_FALLBACK_MODELS, OpenRouterClient
from app.ai.project_index import ProjectIndexStore
from app.ai.prompts import PromptsManager
from app.ai.models_router import models_router
from app.ai.router import ai_router
from app.ai.run_manager import ChatRunManager
from app.auth.router import auth_router
from app.auth.security import (
    LoginRateLimiter,
    PasswordVerifier,
    SecretRedactingFilter,
)
from app.auth.session import SessionManager
from app.config import AppSettings, bootstrap_data_dir
from app.config_secrets import SecretsStore
from app.db import DatabaseManager
from app.github.router import github_router
from app.grounding.overlap_checker import (
    check_copy_overlap,
    extract_claim_evidence,
)
from app.grounding.pdf_indexer import IndexedPdfChunk, PdfIndexer
from app.grounding.router import grounding_router
from app.grounding.scholarly_client import ScholarlyClient
from app.history.git_snapshots import GitSnapshotService
from app.history.router import history_router
from app.latex.compiler import CompileOrchestrator, CompileReport
from app.latex.router import latex_router
from app.latex.sandbox import probe_bwrap_support
from app.latex.synctex import SyncTexService
from app.projects.router import projects_router
from app.projects.service import ProjectService
from app.review_studio.report_store import ReviewReportStore
from app.review_studio.router import review_studio_router
from app.review_studio.text_review import TextReviewOrchestrator
from app.review_studio.visual_inspector import VisualPageInspector
from app.tools.router import tools_router
from app.zotero.acceptance import CitationAcceptanceCoordinator
from app.zotero.router import zotero_router
from app.zotero.staging_repo import CandidateStagingRepository


def create_app(
    settings: AppSettings | None = None,
    frontend_dist_dir: Path | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application instance."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_settings = (
            bootstrap_data_dir(settings.data_dir)
            if settings is not None
            else bootstrap_data_dir()
        )
        if settings is not None:
            active_settings = settings.model_copy(update={"data_dir": active_settings.data_dir})

        db_manager = DatabaseManager(
            db_path=active_settings.db_path,
            backup_dir=active_settings.backups_dir,
        )
        schema_version = db_manager.initialize()
        bwrap_ok = probe_bwrap_support()
        sandbox_mode = "bwrap" if bwrap_ok else "uid_rlimit"

        secrets_store = SecretsStore(secrets_path=active_settings.secrets_yaml_path)
        password_verifier = PasswordVerifier(
            raw_or_hashed_password=active_settings.workspace_password,
            settings_yaml_path=active_settings.settings_yaml_path,
        )
        rate_limiter = LoginRateLimiter()
        session_manager = SessionManager(
            db=db_manager,
            secrets=secrets_store,
            idle_timeout_sec=active_settings.session_idle_timeout_seconds,
            trusted_proxy_ips=active_settings.trusted_proxy_ips,
        )
        secret_redactor = SecretRedactingFilter(
            secrets=[
                *secrets_store.get_redactable_values(),
                *(
                    [active_settings.workspace_password]
                    if active_settings.workspace_password
                    else []
                ),
            ]
        )
        logging.getLogger().addFilter(secret_redactor)
        for handler in logging.root.handlers:
            handler.addFilter(secret_redactor)

        app.state.settings = active_settings
        app.state.db = db_manager
        app.state.schema_version = schema_version
        app.state.sandbox_mode = sandbox_mode
        app.state.secrets_store = secrets_store
        app.state.password_verifier = password_verifier
        app.state.rate_limiter = rate_limiter
        app.state.session_manager = session_manager
        app.state.secret_redactor = secret_redactor
        git_snapshots = GitSnapshotService()
        app.state.git_snapshots = git_snapshots

        async def _on_compile_success(project_id: str, project_dir: Path, report: CompileReport) -> None:
            try:
                await asyncio.to_thread(
                    git_snapshots.create_snapshot,
                    project_dir=project_dir,
                    trigger="compile_success",
                    message=f"Successful compilation of {report.root_tex} ({report.duration_ms}ms)",
                )
            except Exception:
                logging.getLogger(__name__).warning("Failed to create compile snapshot for %s", project_id, exc_info=True)

        app.state.compile_orchestrator = CompileOrchestrator(
            settings=active_settings,
            on_compile_success=_on_compile_success,
        )
        app.state.synctex_service = SyncTexService()
        app.state.project_service = ProjectService(
            data_dir=active_settings.data_dir,
            db=db_manager,
        )

        async def _on_ai_accept(project_dir: Path, label: str) -> None:
            try:
                await asyncio.to_thread(
                    git_snapshots.create_snapshot,
                    project_dir=project_dir,
                    trigger="ai_accept",
                    message=label,
                )
            except Exception:
                logging.getLogger(__name__).warning("Failed to create AI accept snapshot", exc_info=True)

        staging_repo = CandidateStagingRepository(
            db=db_manager,
            ttl_days=active_settings.candidate_ttl_days,
        )
        app.state.staging_repo = staging_repo
        app.state.acceptance_coordinator = CitationAcceptanceCoordinator(
            staging_repo=staging_repo,
            on_ai_accept=_on_ai_accept,
        )
        scholarly_client = ScholarlyClient(
            db=db_manager,
            secrets=secrets_store,
            settings=active_settings,
        )
        pdf_indexer = PdfIndexer(
            db=db_manager,
            settings=active_settings,
        )
        app.state.scholarly_client = scholarly_client
        app.state.pdf_indexer = pdf_indexer

        def _grounding_hook(project_id: str, hunk: ProposedHunk) -> ProposedHunk:
            try:
                corpus_chunks: list[IndexedPdfChunk] = []
                abstracts: list[dict[str, Any]] = []
                with closing(db_manager.get_connection()) as conn:
                    chunk_rows = conn.execute(
                        """
                        SELECT doc_id, project_id, cite_key, page_number, text, injection_flagged, injection_matches_json
                        FROM pdf_chunks
                        WHERE project_id = ?
                        ORDER BY page_number ASC;
                        """,
                        (project_id,),
                    ).fetchall()
                    for r in chunk_rows:
                        corpus_chunks.append(
                            IndexedPdfChunk(
                                doc_id=str(r["doc_id"]),
                                cite_key=str(r["cite_key"]) if r["cite_key"] else None,
                                page_number=int(r["page_number"]),
                                text=str(r["text"]),
                                injection_flagged=bool(r["injection_flagged"]),
                                injection_matches=[],
                            )
                        )
                    cand_rows = conn.execute(
                        """
                        SELECT candidate_id, cite_key, abstract, title
                        FROM candidate_sources
                        WHERE project_id = ? AND status IN ('staged', 'accepted');
                        """,
                        (project_id,),
                    ).fetchall()
                    for r in cand_rows:
                        if r["abstract"]:
                            abstracts.append(
                                {
                                    "candidate_id": str(r["candidate_id"]),
                                    "cite_key": str(r["cite_key"]),
                                    "abstract": str(r["abstract"]),
                                }
                            )
                overlaps = check_copy_overlap(
                    generated_text=hunk.replacement_text,
                    corpus_chunks=corpus_chunks,
                    candidate_and_bib_abstracts=abstracts,
                    settings=active_settings,
                )
                evidence = extract_claim_evidence(
                    generated_text=hunk.replacement_text,
                    project_id=project_id,
                    db=db_manager,
                )
                from dataclasses import asdict

                hunk.overlap_warnings = [asdict(m) for m in overlaps]
                hunk.claim_evidence = [asdict(e) for e in evidence]
                hunk.injection_warning = any(e.injection_warning for e in evidence)
            except Exception:
                logging.getLogger(__name__).warning("Error executing grounding hook for %s", project_id, exc_info=True)
            return hunk

        prompts_manager = PromptsManager(prompts_path=active_settings.prompts_yaml_path)
        model_registry = ModelRegistry(db_manager)
        model_registry.seed_if_needed(
            build_seed_models(active_settings.model_allowlist, DEFAULT_FALLBACK_MODELS)
        )
        app.state.model_registry = model_registry
        openrouter_client = OpenRouterClient(
            db=db_manager,
            secrets=secrets_store,
            settings=active_settings,
            registry=model_registry,
        )
        for m in model_registry.list_enabled():
            openrouter_client.set_model_pricing(
                m.id, m.prompt_price_per_token, m.completion_price_per_token
            )
        app.state.prompts_manager = prompts_manager
        app.state.openrouter_client = openrouter_client
        project_index_store = ProjectIndexStore(db_manager, app.state.project_service)
        app.state.project_index_store = project_index_store
        app.state.chat_service = ChatService(
            db=db_manager,
            project_service=app.state.project_service,
            prompts_manager=prompts_manager,
            openrouter=openrouter_client,
            settings=active_settings,
            grounding_hook=_grounding_hook,
            project_index_store=project_index_store,
        )
        app.state.chat_run_manager = ChatRunManager()
        app.state.text_review_orchestrator = TextReviewOrchestrator(
            openrouter=openrouter_client,
            prompts_mgr=prompts_manager,
            settings=active_settings,
        )
        app.state.visual_inspector = VisualPageInspector()
        app.state.review_report_store = ReviewReportStore()
        try:
            yield
        finally:
            await app.state.chat_run_manager.stop_all()
            await scholarly_client.aclose()
            logging.getLogger().removeFilter(secret_redactor)
            for handler in logging.root.handlers:
                try:
                    handler.removeFilter(secret_redactor)
                except Exception:
                    pass
            try:
                conn = db_manager.get_connection()
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                finally:
                    conn.close()
            except Exception:
                pass

    app = FastAPI(
        title="Lemma",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(latex_router)
    app.include_router(ai_router)
    app.include_router(models_router)
    app.include_router(zotero_router)
    app.include_router(github_router)
    app.include_router(grounding_router)
    app.include_router(history_router)
    app.include_router(review_studio_router)
    app.include_router(tools_router)

    @app.get("/api/health")
    async def get_health() -> dict[str, str | int]:
        schema_version = getattr(app.state, "schema_version", 1)
        sandbox_mode = getattr(
            app.state,
            "sandbox_mode",
            "bwrap" if probe_bwrap_support() else "uid_rlimit",
        )
        return {
            "status": "ok",
            "schema_version": schema_version,
            "sandbox_mode": sandbox_mode,
        }

    resolved_dist = (
        Path(frontend_dist_dir)
        if frontend_dist_dir is not None
        else Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    )
    if resolved_dist.is_dir() and (resolved_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(resolved_dist), html=True), name="frontend")

    return app
