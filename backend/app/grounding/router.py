from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.router import require_authenticated_session, require_csrf
from app.auth.session import SessionRecord
from app.config import AppSettings
from app.grounding.citation_verifier import parse_bib_file
from app.grounding.overlap_checker import (
    check_copy_overlap,
    extract_claim_evidence,
)
from app.grounding.pdf_indexer import IndexedPdfChunk, PdfIndexer
from app.grounding.scholarly_client import ScholarlyClient, ScholarlyWork
from app.projects.service import ProjectService
from app.zotero.staging_repo import CandidateStagingRepository

grounding_router = APIRouter(tags=["grounding"])


class StageWorkPayload(BaseModel):
    work: dict[str, Any]


class IndexPdfPayload(BaseModel):
    cite_key: str
    file_path: str | None = None


class VerifyDraftPayload(BaseModel):
    draft_text: str
    file_path: str | None = None


def _derive_cite_key(work: ScholarlyWork | dict[str, Any]) -> str:
    authors = work.authors if isinstance(work, ScholarlyWork) else work.get("authors", [])
    year = work.year if isinstance(work, ScholarlyWork) else work.get("year")
    author_str = "ref"
    if authors:
        first = str(authors[0]).strip()
        if "," in first:
            author_str = first.split(",")[0].strip()
        else:
            author_str = first.split()[-1].strip()
    clean_author = "".join(ch for ch in author_str if ch.isalnum()).lower() or "ref"
    year_str = str(year) if year else "2024"
    return f"{clean_author}{year_str}"


def _build_default_bibtex(cite_key: str, work: ScholarlyWork | dict[str, Any]) -> str:
    title = work.title if isinstance(work, ScholarlyWork) else work.get("title", "")
    authors = work.authors if isinstance(work, ScholarlyWork) else work.get("authors", [])
    year = work.year if isinstance(work, ScholarlyWork) else work.get("year")
    doi = work.doi if isinstance(work, ScholarlyWork) else work.get("doi")
    authors_str = " and ".join(authors) if authors else "Unknown"
    lines = [
        f"@article{{{cite_key},",
        f"  title = {{{title}}},",
        f"  author = {{{authors_str}}},",
    ]
    if year:
        lines.append(f"  year = {{{year}}},")
    if doi:
        lines.append(f"  doi = {{{doi}}},")
    lines.append("}")
    return "\n".join(lines)


@grounding_router.get("/api/projects/{project_id}/grounding/search")
async def search_scholarly_works(
    project_id: str,
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    scholarly_client: ScholarlyClient = request.app.state.scholarly_client
    try:
        await asyncio.to_thread(project_service.get_project, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    works = await scholarly_client.search_works(query=q, limit=limit)
    return {"works": [asdict(w) if isinstance(w, ScholarlyWork) else w for w in works]}


@grounding_router.post("/api/projects/{project_id}/grounding/stage")
async def stage_scholarly_work(
    project_id: str,
    payload: StageWorkPayload,
    request: Request,
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

    raw_work = payload.work
    raw_year = raw_work.get("year")
    year_val: int | None = None
    if raw_year is not None:
        try:
            year_val = int(raw_year)
        except (ValueError, TypeError):
            year_val = None

    work = ScholarlyWork(
        title=str(raw_work.get("title", "Untitled")),
        authors=[str(a) for a in raw_work.get("authors", [])],
        year=year_val,
        doi=str(raw_work["doi"]) if raw_work.get("doi") else None,
        arxiv_id=str(raw_work["arxiv_id"]) if raw_work.get("arxiv_id") else None,
        abstract=str(raw_work["abstract"]) if raw_work.get("abstract") else None,
        oa_pdf_url=str(raw_work["oa_pdf_url"]) if raw_work.get("oa_pdf_url") else None,
        source_api=str(raw_work.get("source_api", "semantic_scholar")),
        metadata_verified=bool(raw_work.get("metadata_verified", True)),
        injection_flagged=bool(raw_work.get("injection_flagged", False)),
    )

    base_cite_key = _derive_cite_key(work)
    bibtex = _build_default_bibtex(base_cite_key, work)

    # Load existing bib entries to disambiguate keys if needed
    existing_bib_entries = {}
    try:
        bib_file = await asyncio.to_thread(project_service.read_file, project_id, "references.bib")
        existing_bib_entries = parse_bib_file(bib_file.text)
    except Exception:
        existing_bib_entries = {}

    cand = await asyncio.to_thread(
        staging_repo.stage_candidate,
        project_id=project_id,
        cite_key=base_cite_key,
        title=work.title,
        authors=work.authors,
        year=work.year,
        doi=work.doi,
        abstract=work.abstract,
        source_api=work.source_api,
        bibtex=bibtex,
        grounding_tier="full_text_pdf" if work.oa_pdf_url else "abstract_only",
        existing_bib_entries=existing_bib_entries,
    )

    return {"candidate": asdict(cand)}


@grounding_router.post("/api/projects/{project_id}/grounding/index-pdf")
async def index_grounding_pdf(
    project_id: str,
    payload: IndexPdfPayload,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    pdf_indexer: PdfIndexer = request.app.state.pdf_indexer
    try:
        project_dir = await asyncio.to_thread(project_service.get_project_dir, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    pdf_path: Path | None = None
    if payload.file_path:
        clean_rel = Path(payload.file_path)
        if clean_rel.is_absolute() or ".." in clean_rel.parts:
            raise HTTPException(status_code=400, detail="Invalid PDF file path")
        target_path = (project_dir / clean_rel).resolve()
        try:
            target_path.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="PDF path traverses outside project")
        pdf_path = target_path
    else:
        # Check candidate source pdf_path
        staging_repo: CandidateStagingRepository = request.app.state.staging_repo
        cands = await asyncio.to_thread(staging_repo.list_active_candidates, project_id)
        for c in cands:
            if c.cite_key == payload.cite_key and c.pdf_path:
                pdf_path = project_dir / c.pdf_path
                break

    if not pdf_path or not pdf_path.is_file():
        raise HTTPException(status_code=404, detail=f"PDF file not found for cite_key '{payload.cite_key}'")

    chunks = await asyncio.to_thread(
        pdf_indexer.index_pdf,
        project_id=project_id,
        pdf_path=pdf_path,
        cite_key=payload.cite_key,
    )
    return {"chunks_indexed": len(chunks), "cite_key": payload.cite_key}


@grounding_router.post("/api/projects/{project_id}/grounding/verify-draft")
async def verify_draft_grounding(
    project_id: str,
    payload: VerifyDraftPayload,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    project_service: ProjectService = request.app.state.project_service
    db = request.app.state.db
    settings: AppSettings = request.app.state.settings

    try:
        await asyncio.to_thread(project_service.get_project, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Load corpus chunks from pdf_chunks
    corpus_chunks: list[IndexedPdfChunk] = []
    abstracts: list[dict[str, Any]] = []

    with closing(db.get_connection()) as conn:
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

    overlap_warnings = check_copy_overlap(
        generated_text=payload.draft_text,
        corpus_chunks=corpus_chunks,
        candidate_and_bib_abstracts=abstracts,
        settings=settings,
    )
    claim_evidence = extract_claim_evidence(
        generated_text=payload.draft_text,
        project_id=project_id,
        db=db,
    )

    return {
        "overlap_warnings": [asdict(m) for m in overlap_warnings],
        "claim_evidence": [asdict(e) for e in claim_evidence],
    }
