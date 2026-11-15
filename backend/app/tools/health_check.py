from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.db import DatabaseManager
from app.grounding.citation_verifier import (
    parse_bib_file,
    strip_latex_comments_and_verbatim,
    extract_cite_keys,
    _SINGLE_CITE_CMD_RE,
    _MULTI_CITE_CMD_RE,
)
from app.grounding.pdf_indexer import scan_for_prompt_injection

logger = logging.getLogger(__name__)


@dataclass
class HealthCheckReport:
    missing_bib_keys: list[dict[str, Any]] = field(default_factory=list)
    uncited_bib_keys: list[str] = field(default_factory=list)
    uncited_factual_claims: list[dict[str, Any]] = field(default_factory=list)
    abstract_only_citations: list[str] = field(default_factory=list)
    injection_warnings: list[dict[str, Any]] = field(default_factory=list)


_INPUT_CMD_RE = re.compile(
    r"\\(?:input|include|subfile)\*?(?:\[[^\]]*\])?\{([^}]+)\}"
)

_NOCITE_STAR_RE = re.compile(r"\\nocite\*?\s*\{(?:\s*\*|\s*all\s*)\}")

_EMPIRICAL_CLAIM_RE = re.compile(
    r"\b(?:"
    r"\d+(?:\.\d+)?\s*%"
    r"|\d+(?:\.\d+)?\s*(?:percent|percentage\s*points?)"
    r"|p\s*[<>=]\s*0?\.\d+"
    r"|statistically\s+significant"
    r"|outperform(?:s|ed|ing)?"
    r"|improves?\s+(?:accuracy|performance|efficiency|latency|throughput|precision|recall|f1|auc|score)"
    r"|improves?\s+by\s+\d+"
    r"|achieves?\s+(?:state-of-the-art|sota|accuracy|f1|precision|recall|auc|better|superior|higher|lower)"
    r"|reduces?\s+(?:latency|error|loss|energy|time)\s+by"
    r"|increased?\s+by\s+\d+"
    r"|decreased?\s+by\s+\d+"
    r")\b",
    re.IGNORECASE,
)

_CITE_PATTERN_RE = re.compile(r"\\[a-zA-Z]*cite[a-zA-Z]*\*?", re.IGNORECASE)


def _resolve_tex_path(base_dir: Path, current_file: Path, raw_target: str) -> Path | None:
    cleaned = raw_target.strip()
    if not cleaned:
        return None

    candidates = [
        current_file.parent / cleaned,
        base_dir / cleaned,
    ]
    if not Path(cleaned).suffix:
        candidates = [
            current_file.parent / f"{cleaned}.tex",
            base_dir / f"{cleaned}.tex",
            *candidates,
        ]

    for cand in candidates:
        try:
            cand_res = cand.resolve()
            if cand_res.is_file() and cand_res.is_relative_to(base_dir.resolve()):
                # Reject external symlinks
                if cand.is_symlink() and not cand.resolve().is_relative_to(base_dir.resolve()):
                    continue
                return cand_res
        except (ValueError, OSError):
            continue
    return None


def run_citation_health_check(
    project_dir: Path,
    root_tex: str = "main.tex",
    db: DatabaseManager | None = None,
    project_id: str | None = None,
) -> HealthCheckReport:
    """Walks the full \\input{}/\\include{} tree and audits citations, empirical claims, and grounding."""
    project_dir = Path(project_dir).resolve()
    resolved_root = (project_dir / root_tex).resolve()
    if not resolved_root.is_relative_to(project_dir):
        raise PermissionError(f"root_tex escapes project root: {root_tex}")

    if not resolved_root.is_file():
        # Fall back to any .tex in project_dir
        tex_files = [f for f in project_dir.glob("*.tex") if f.resolve().is_relative_to(project_dir)]
        if tex_files:
            resolved_root = tex_files[0].resolve()
        else:
            return HealthCheckReport()

    visited_files: set[Path] = set()
    files_to_visit: list[Path] = [resolved_root]

    all_cited_keys: set[str] = set()
    cited_key_occurrences: list[dict[str, Any]] = []
    has_nocite_star = False
    uncited_factual_claims: list[dict[str, Any]] = []
    injection_warnings: list[dict[str, Any]] = []

    # Walk document tree
    while files_to_visit:
        curr_file = files_to_visit.pop(0)
        if curr_file in visited_files:
            continue
        visited_files.add(curr_file)

        try:
            rel_path = curr_file.relative_to(project_dir).as_posix()
        except ValueError:
            rel_path = curr_file.name

        try:
            raw_text = curr_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Check prompt injection in source tex
        inj_matches = scan_for_prompt_injection(raw_text)
        if inj_matches:
            injection_warnings.append({
                "type": "tex_source",
                "file": rel_path,
                "matches": inj_matches,
            })

        if _NOCITE_STAR_RE.search(raw_text):
            has_nocite_star = True

        lines = raw_text.splitlines()
        for line_idx, line in enumerate(lines, start=1):
            cleaned_line = strip_latex_comments_and_verbatim(line)
            if not cleaned_line.strip():
                continue

            # Check included files
            for m in _INPUT_CMD_RE.finditer(cleaned_line):
                target_str = m.group(1).strip()
                resolved_child = _resolve_tex_path(project_dir, curr_file, target_str)
                if resolved_child and resolved_child not in visited_files:
                    files_to_visit.append(resolved_child)

            # Check cite keys on this line
            line_keys = extract_cite_keys(cleaned_line)
            for k in line_keys:
                all_cited_keys.add(k)
                cited_key_occurrences.append({
                    "key": k,
                    "file": rel_path,
                    "line": line_idx,
                })

            # Check empirical claims without citations
            # Split line into sentence-like clauses
            sentences = re.split(r"(?<=[.!?])\s+", cleaned_line)
            for sent in sentences:
                sent_stripped = sent.strip()
                if not sent_stripped or sent_stripped.startswith("\\"):
                    continue
                # If sentence matches empirical claim pattern
                if _EMPIRICAL_CLAIM_RE.search(sent_stripped):
                    # Check if there is any citation in this sentence
                    if not _CITE_PATTERN_RE.search(sent_stripped):
                        uncited_factual_claims.append({
                            "sentence": sent_stripped,
                            "claim": sent_stripped,
                            "file": rel_path,
                            "line": line_idx,
                        })

    # Parse bib files in project
    all_bib_entries: dict[str, Any] = {}
    bib_files = list(project_dir.glob("*.bib"))
    for bib_file in bib_files:
        try:
            content = bib_file.read_text(encoding="utf-8", errors="replace")
            entries = parse_bib_file(content)
            all_bib_entries.update(entries)
        except Exception:
            continue

    # Determine missing bib keys
    missing_bib_keys: list[dict[str, Any]] = []
    seen_missing: set[str] = set()
    for occ in cited_key_occurrences:
        k = occ["key"]
        if k not in all_bib_entries:
            if k not in seen_missing:
                seen_missing.add(k)
                missing_bib_keys.append(occ)

    # Determine uncited bib keys
    uncited_bib_keys: list[str] = []
    if not has_nocite_star:
        for k in all_bib_entries.keys():
            if k not in all_cited_keys:
                uncited_bib_keys.append(k)

    # Check database for candidate grounding tiers and injections
    abstract_only_citations: list[str] = []
    proj_id = project_id or project_dir.name

    if db is not None:
        try:
            with contextlib.closing(db.get_connection()) as conn:
                # Query candidate sources for abstract-only citations
                for k in all_cited_keys:
                    row = conn.execute(
                        "SELECT grounding_tier FROM candidate_sources WHERE project_id = ? AND cite_key = ? LIMIT 1",
                        (proj_id, k),
                    ).fetchone()
                    if row and row["grounding_tier"] == "abstract_only":
                        abstract_only_citations.append(k)
                    elif row is None and k in all_bib_entries:
                        # Check if pdf is indexed in pdf_documents
                        doc_row = conn.execute(
                            "SELECT doc_id FROM pdf_documents WHERE project_id = ? AND cite_key = ? LIMIT 1",
                            (proj_id, k),
                        ).fetchone()
                        # If candidate entry not in db and no indexed pdf, check if bib entry has abstract only
                        bib_entry = all_bib_entries[k]
                        if not doc_row and getattr(bib_entry, "abstract", None) and not (project_dir / "project-uploads" / f"{k}.pdf").is_file():
                            abstract_only_citations.append(k)

                # Query pdf chunks for prompt injections in cited papers
                for k in all_cited_keys:
                    flagged_chunks = conn.execute(
                        "SELECT doc_id, page_number, injection_matches_json FROM pdf_chunks WHERE project_id = ? AND cite_key = ? AND injection_flagged = 1",
                        (proj_id, k),
                    ).fetchall()
                    for fc in flagged_chunks:
                        try:
                            matches = json.loads(fc["injection_matches_json"])
                        except Exception:
                            matches = []
                        injection_warnings.append({
                            "type": "pdf_chunk",
                            "cite_key": k,
                            "doc_id": fc["doc_id"],
                            "page_number": fc["page_number"],
                            "matches": matches,
                        })
        except Exception as e:
            logger.warning("Error querying db during health check for %s: %s", proj_id, e)

    return HealthCheckReport(
        missing_bib_keys=missing_bib_keys,
        uncited_bib_keys=uncited_bib_keys,
        uncited_factual_claims=uncited_factual_claims,
        abstract_only_citations=abstract_only_citations,
        injection_warnings=injection_warnings,
    )
