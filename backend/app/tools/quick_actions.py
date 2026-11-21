from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import secrets as pysecrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.ai.chat_service import ProposedHunk
from app.ai.openrouter import OpenRouterClient, OpenRouterError
from app.ai.output_contracts import (
    EXPLAIN_MATH_CONTRACT,
    QUICK_EDIT_CONTRACT,
    compose_system_prompt,
)
from app.ai.prompts import PromptsManager
from app.config import AppSettings
from app.db import DatabaseManager
from app.grounding.citation_verifier import BibEntry, verify_citations
from app.grounding.overlap_checker import (
    ClaimEvidence,
    check_copy_overlap,
    extract_claim_evidence,
)
from app.grounding.pdf_indexer import IndexedPdfChunk, scan_for_prompt_injection
from app.integrity.classifier import classify_hunk, classify_turn
from app.integrity.prompts import build_integrity_overlay

logger = logging.getLogger(__name__)

QuickActionType = Literal["tighten", "verify_claim", "formal_tone", "explain_math"]


@dataclass
class QuickActionResult:
    action: QuickActionType
    hunk: ProposedHunk | None = None
    explanation: str | None = None
    claim_evidence: list[dict[str, Any]] = field(default_factory=list)
    injection_warning: bool = False
    warning_message: str | None = None
    integrity: dict[str, Any] | None = None


def _strip_markdown_fences(content: str) -> str:
    cleaned = content.strip()
    match = re.search(r"```(?:latex|tex)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return cleaned


def _compute_file_etag(file_path: Path) -> str:
    try:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError:
        return ""


async def run_selection_quick_action(
    project_id: str,
    project_dir: Path,
    file_path: str,
    start_line: int,
    end_line: int,
    selected_text: str,
    action: QuickActionType,
    model: str = "anthropic/claude-3.7-sonnet",
    openrouter: OpenRouterClient | None = None,
    db: DatabaseManager | None = None,
    settings: AppSettings | None = None,
    bib_entries: dict[str, BibEntry] | None = None,
    staged_keys: set[str] | None = None,
    prompts_manager: PromptsManager | None = None,
    academic_integrity_mode: bool = False,
) -> QuickActionResult:
    """Executes an inline academic quick action (tighten, formal tone, verify claim, explain math)."""
    project_dir = Path(project_dir).resolve()
    full_path = (project_dir / file_path).resolve()
    if not full_path.is_relative_to(project_dir):
        raise PermissionError(f"file_path escapes project root: {file_path}")
    base_etag = _compute_file_etag(full_path) if full_path.is_file() else ""

    # Sanitize inner delimiter tags from selected text
    safe_selected_text = re.sub(
        r"</?\s*untrusted_source_excerpt\b[^>]*>",
        "",
        selected_text,
        flags=re.IGNORECASE,
    )

    # Scan selection for prompt injection
    inj_matches = scan_for_prompt_injection(safe_selected_text)
    injection_warning = len(inj_matches) > 0

    delimited_text = (
        f"<untrusted_source_excerpt>\n{safe_selected_text}\n</untrusted_source_excerpt>"
        if injection_warning
        else safe_selected_text
    )

    if action in ("tighten", "formal_tone"):
        if openrouter is None:
            raise RuntimeError("OpenRouter client is required for text editing actions.")

        if prompts_manager is None:
            raise RuntimeError("PromptsManager is required for text editing actions.")

        profile = prompts_manager.get_profile(action)
        system_prompt = compose_system_prompt(profile.system_prompt, QUICK_EDIT_CONTRACT)
        if academic_integrity_mode:
            overlay = build_integrity_overlay(profile.role)
            if overlay:
                system_prompt = f"{system_prompt}\n\n{overlay}"
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Please revise this LaTeX passage:\n\n{delimited_text}",
            },
        ]

        res = await openrouter.complete(model=model, messages=messages, max_tokens=8192)
        if res.finish_reason == "length":
            raise OpenRouterError(
                "Model output was truncated before completing the revision."
            )
        replacement_text = _strip_markdown_fences(res.content)
        if not replacement_text.strip():
            raise OpenRouterError("Model returned an empty revision.")

        # Verify citations
        verification = verify_citations(
            replacement_text,
            bib_entries=bib_entries or {},
            staged_keys=staged_keys or set(),
        )

        # Check copy overlap if db is available
        corpus_chunks: list[IndexedPdfChunk] = []
        abstracts: list[dict[str, Any]] = []
        if db is not None:
            try:
                with contextlib.closing(db.get_connection()) as conn:
                    # Query non-flagged pdf chunks
                    rows = conn.execute(
                        "SELECT doc_id, cite_key, page_number, text, injection_flagged FROM pdf_chunks WHERE project_id = ? AND injection_flagged = 0",
                        (project_id,),
                    ).fetchall()
                    for r in rows:
                        corpus_chunks.append(
                            IndexedPdfChunk(
                                doc_id=r["doc_id"],
                                cite_key=r["cite_key"],
                                page_number=r["page_number"],
                                text=r["text"],
                                injection_flagged=bool(r["injection_flagged"]),
                                injection_matches=[],
                            )
                        )
                    # Query candidate abstracts
                    cand_rows = conn.execute(
                        "SELECT candidate_id, cite_key, abstract FROM candidate_sources WHERE project_id = ? AND abstract IS NOT NULL",
                        (project_id,),
                    ).fetchall()
                    for cr in cand_rows:
                        abstracts.append({
                            "cite_key": cr["cite_key"],
                            "doc_id": cr["candidate_id"],
                            "abstract": cr["abstract"],
                        })
            except Exception as e:
                logger.warning("Error querying pdf_chunks or candidate_sources in quick action for %s: %s", project_id, e)

        if bib_entries:
            for k, entry in bib_entries.items():
                if entry.abstract:
                    abstracts.append({
                        "cite_key": k,
                        "doc_id": f"bib_{k}",
                        "abstract": entry.abstract,
                    })

        overlap_matches = check_copy_overlap(
            generated_text=replacement_text,
            corpus_chunks=corpus_chunks,
            candidate_and_bib_abstracts=abstracts,
            settings=settings,
        )

        overlap_warnings = [
            {
                "matched_text": m.matched_text,
                "word_count": m.word_count,
                "cite_key": m.cite_key,
                "doc_id": m.doc_id,
                "page_number": m.page_number,
            }
            for m in overlap_matches
        ]

        integrity_dict: dict[str, Any] | None = None
        if academic_integrity_mode:
            assessment = classify_hunk(
                profile_id=action,
                original_text=selected_text,
                replacement_text=replacement_text,
                cite_keys=verification.valid_keys + verification.staged_keys,
                unknown_cite_keys=verification.unknown_keys,
                overlap_warnings=overlap_warnings,
            )
            integrity_dict = assessment.to_dict()

        hunk = ProposedHunk(
            hunk_id=f"hunk_{pysecrets.token_hex(8)}",
            file_path=file_path,
            base_etag=base_etag,
            start_line=start_line,
            end_line=end_line,
            original_text=selected_text,
            replacement_text=replacement_text,
            cite_keys=verification.valid_keys + verification.staged_keys,
            unknown_cite_keys=verification.unknown_keys,
            overlap_warnings=overlap_warnings,
            injection_warning=injection_warning,
            integrity=integrity_dict,
        )

        return QuickActionResult(
            action=action,
            hunk=hunk,
            injection_warning=injection_warning,
            integrity=integrity_dict,
        )

    elif action == "verify_claim":
        evidence_dicts: list[dict[str, Any]] = []
        any_flagged = injection_warning

        if db is not None:
            evidences = extract_claim_evidence(selected_text, project_id=project_id, db=db)
            for ev in evidences:
                if ev.injection_warning:
                    any_flagged = True
                # Format snippet with delimiter
                safe_snippet = f"<untrusted_source_excerpt>\n{ev.snippet}\n</untrusted_source_excerpt>"
                evidence_dicts.append({
                    "claim_text": ev.claim_text,
                    "cite_key": ev.cite_key,
                    "grounding_tier": ev.grounding_tier,
                    "snippet": safe_snippet,
                    "page_number": ev.page_number,
                    "injection_warning": ev.injection_warning,
                })

        return QuickActionResult(
            action="verify_claim",
            claim_evidence=evidence_dicts,
            injection_warning=any_flagged,
        )

    elif action == "explain_math":
        if openrouter is None:
            raise RuntimeError("OpenRouter client is required for math explanation.")

        if prompts_manager is None:
            raise RuntimeError("PromptsManager is required for math explanation.")

        explain_profile = prompts_manager.get_profile("explain_math")
        system_prompt = compose_system_prompt(
            explain_profile.system_prompt, EXPLAIN_MATH_CONTRACT
        )
        if academic_integrity_mode:
            overlay = build_integrity_overlay(explain_profile.role)
            if overlay:
                system_prompt = f"{system_prompt}\n\n{overlay}"
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Please explain this LaTeX mathematics:\n\n{delimited_text}",
            },
        ]

        res = await openrouter.complete(model=model, messages=messages, max_tokens=8192)
        if res.finish_reason == "length":
            raise OpenRouterError(
                "Model output was truncated before completing the explanation."
            )
        if not res.content.strip():
            raise OpenRouterError("Model returned an empty explanation.")
        integrity_dict: dict[str, Any] | None = None
        if academic_integrity_mode:
            assessment = classify_turn(
                profile_id="explain_math",
                user_prompt=selected_text,
                reply=res.content,
                selection_text=selected_text,
            )
            integrity_dict = assessment.to_dict()
        return QuickActionResult(
            action="explain_math",
            explanation=res.content,
            injection_warning=injection_warning,
            integrity=integrity_dict,
        )

    raise ValueError(f"Unknown quick action: {action}")
