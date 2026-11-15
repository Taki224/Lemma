from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.grounding.citation_verifier import parse_bib_file
from app.projects.service import ProjectService
from app.zotero.client import ZoteroClient
from app.zotero.staging_repo import (
    CandidateSource,
    CandidateStagingRepository,
    disambiguate_cite_key,
    normalize_doi,
    rewrite_bibtex_cite_key,
)


@dataclass(frozen=True)
class AcceptanceResult:
    accepted_cite_keys: list[str]
    rejected_cite_keys: list[str]
    key_remapping: dict[str, str] = field(default_factory=dict)
    zotero_pushed_keys: list[str] = field(default_factory=list)
    bib_etag: str | None = None
    warning_message: str | None = None


class CitationAcceptanceCoordinator:
    """Atomic per-citation acceptance/rejection with ETag-protected .bib writes and key-collision remapping."""

    def __init__(
        self,
        staging_repo: CandidateStagingRepository,
        on_ai_accept: Callable[[Path, str], Any] | None = None,
    ) -> None:
        self.staging_repo = staging_repo
        self.on_ai_accept = on_ai_accept

    async def accept_hunk_citations(
        self,
        project_id: str,
        project_dir: Path,
        project_service: ProjectService,
        accepted_cite_keys: list[str],
        rejected_cite_keys: list[str],
        zotero_client: ZoteroClient | None,
        collection_key: str | None,
        push_enabled: bool = False,
    ) -> AcceptanceResult:
        # 1. Process rejected citations first without ever touching references.bib or Zotero
        final_rejected: list[str] = []
        for rej_key in rejected_cite_keys:
            clean_rej = rej_key.strip()
            if not clean_rej:
                continue
            self.staging_repo.mark_status(project_id, clean_rej, "rejected")
            final_rejected.append(clean_rej)

        clean_accepted_input = [k.strip() for k in accepted_cite_keys if k.strip()]
        if not clean_accepted_input:
            return AcceptanceResult(
                accepted_cite_keys=[],
                rejected_cite_keys=final_rejected,
                key_remapping={},
                zotero_pushed_keys=[],
                bib_etag=None,
            )

        # 2. Read current references.bib and ETag for optimistic concurrency protection
        try:
            bib_file = project_service.read_file(project_id, "references.bib")
            current_bib_text = bib_file.text
            current_etag: str | None = bib_file.etag
        except FileNotFoundError:
            current_bib_text = ""
            current_etag = None

        existing_entries = parse_bib_file(current_bib_text)
        taken_keys: set[str] = set(existing_entries.keys())
        existing_doi_to_key: dict[str, str] = {}
        for k, entry in existing_entries.items():
            norm_d = normalize_doi(entry.doi)
            if norm_d:
                existing_doi_to_key[norm_d] = k

        key_remapping: dict[str, str] = {}
        final_accepted_keys: list[str] = []
        appended_blocks: list[str] = []
        staged_updates: list[tuple[CandidateSource, str, str]] = []
        unknown_keys: list[str] = []

        for orig_key in clean_accepted_input:
            candidate = self.staging_repo.get_by_cite_key(project_id, orig_key)
            if candidate is None or candidate.status != "staged":
                # If already present in references.bib directly, keep it as accepted
                if orig_key in existing_entries:
                    final_accepted_keys.append(orig_key)
                elif candidate is not None and candidate.status == "accepted":
                    final_accepted_keys.append(candidate.cite_key)
                else:
                    unknown_keys.append(orig_key)
                continue

            cand_doi = normalize_doi(candidate.doi)

            # Check if same DOI already exists in references.bib
            if cand_doi and cand_doi in existing_doi_to_key:
                existing_key = existing_doi_to_key[cand_doi]
                if existing_key != orig_key:
                    key_remapping[orig_key] = existing_key
                staged_updates.append((
                    candidate,
                    existing_key,
                    rewrite_bibtex_cite_key(candidate.bibtex, existing_key),
                ))
                final_accepted_keys.append(existing_key)
                continue

            # If cite key already exists with identical title/author or already in bib
            if orig_key in taken_keys and cand_doi is None:
                existing_item = existing_entries.get(orig_key)
                if existing_item and existing_item.title.strip().lower() == candidate.title.strip().lower():
                    final_accepted_keys.append(orig_key)
                    staged_updates.append((candidate, orig_key, candidate.bibtex))
                    continue

            # Check for last-moment cite_key collision with a different paper in references.bib
            resolved_key = disambiguate_cite_key(orig_key, taken_keys)
            if resolved_key != orig_key:
                key_remapping[orig_key] = resolved_key

            taken_keys.add(resolved_key)
            if cand_doi:
                existing_doi_to_key[cand_doi] = resolved_key

            resolved_bibtex = rewrite_bibtex_cite_key(candidate.bibtex, resolved_key)
            appended_blocks.append(resolved_bibtex)
            staged_updates.append((candidate, resolved_key, resolved_bibtex))
            final_accepted_keys.append(resolved_key)

        new_etag = current_etag
        if appended_blocks:
            base = current_bib_text.rstrip()
            prefix = f"{base}\n\n" if base else ""
            merged_bib = prefix + "\n\n".join(appended_blocks) + "\n"
            write_res = project_service.write_file(
                project_id=project_id,
                rel_path="references.bib",
                content=merged_bib,
                expected_etag=current_etag,
            )
            new_etag = write_res.etag

        # Apply SQLite candidate status updates ONLY after references.bib write succeeds
        accepted_candidates_for_push: list[CandidateSource] = []
        for cand, res_key, res_bib in staged_updates:
            updated_cand = self.staging_repo.update_cite_key_and_status(
                project_id=project_id,
                candidate_id=cand.candidate_id,
                new_cite_key=res_key,
                new_bibtex=res_bib,
                status="accepted",
            )
            if updated_cand is not None:
                accepted_candidates_for_push.append(updated_cand)

        warning_msg: str | None = None
        if unknown_keys:
            warning_msg = f"Citation key(s) not found in staging or references.bib: {', '.join(unknown_keys)}"

        zotero_pushed_keys: list[str] = []
        if push_enabled and zotero_client is not None:
            for cand in accepted_candidates_for_push:
                zot_key = await zotero_client.create_accepted_item(
                    candidate=cand,
                    collection_key=collection_key,
                    push_enabled=True,
                )
                if zot_key:
                    zotero_pushed_keys.append(zot_key)
                elif zotero_client.last_status_warning:
                    warning_msg = zotero_client.last_status_warning

        # 4. Invoke optional Git snapshot hook when AI citations/changes are accepted
        if final_accepted_keys and self.on_ai_accept is not None:
            label = f"Accepted AI citations: {', '.join(final_accepted_keys)}"
            maybe_coro = self.on_ai_accept(project_dir, label)
            if inspect.isawaitable(maybe_coro):
                await maybe_coro

        return AcceptanceResult(
            accepted_cite_keys=final_accepted_keys,
            rejected_cite_keys=final_rejected,
            key_remapping=key_remapping,
            zotero_pushed_keys=zotero_pushed_keys,
            bib_etag=new_etag,
            warning_message=warning_msg,
        )
