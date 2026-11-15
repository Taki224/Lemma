from __future__ import annotations

import json
import re
import secrets as pysecrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, cast

from app.db import DatabaseManager
from app.grounding.citation_verifier import BibEntry

GroundingTier = Literal["full_text_pdf", "abstract_only"]
CandidateStatus = Literal["staged", "accepted", "rejected", "expired"]

_BIB_HEADER_RE = re.compile(r"^(\s*@[a-zA-Z0-9_-]+\s*\{\s*)([^,\s]+)(\s*,)", re.MULTILINE)


@dataclass(frozen=True)
class CandidateSource:
    candidate_id: str
    project_id: str
    cite_key: str
    title: str
    authors: list[str]
    year: int | None
    doi: str | None
    abstract: str | None
    source_api: str
    bibtex: str
    grounding_tier: GroundingTier
    pdf_path: str | None
    created_at: str
    expires_at: str
    status: CandidateStatus


def normalize_doi(raw_doi: str | None) -> str | None:
    if not raw_doi:
        return None
    cleaned = raw_doi.strip()
    cleaned = re.sub(
        r"^(?:(?:https?://)?(?:dx\.)?doi\.org/|doi:\s*)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned.lower() if cleaned else None


def rewrite_bibtex_cite_key(bibtex: str, new_key: str) -> str:
    if not bibtex.strip():
        return f"@article{{{new_key},\n}}"
    if _BIB_HEADER_RE.search(bibtex):
        return _BIB_HEADER_RE.sub(rf"\g<1>{new_key}\g<3>", bibtex, count=1)
    return bibtex


def disambiguate_cite_key(base_key: str, taken_keys: set[str]) -> str:
    clean_base = base_key.strip() or "ref"
    taken_lower = {k.lower() for k in taken_keys}
    if clean_base.lower() not in taken_lower:
        return clean_base
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    for ch in alphabet:
        cand = f"{clean_base}{ch}"
        if cand.lower() not in taken_lower:
            return cand
    idx = 2
    while True:
        cand = f"{clean_base}_{idx}"
        if cand.lower() not in taken_lower:
            return cand
        idx += 1


def _row_to_candidate(row: sqlite3.Row) -> CandidateSource:
    try:
        authors_raw = json.loads(str(row["authors_json"] or "[]"))
        authors = [str(a) for a in authors_raw] if isinstance(authors_raw, list) else []
    except json.JSONDecodeError:
        authors = []
    year_val = int(row["year"]) if row["year"] is not None else None
    tier = str(row["grounding_tier"] or "abstract_only")
    if tier not in {"full_text_pdf", "abstract_only"}:
        tier = "abstract_only"
    status_val = str(row["status"] or "staged")
    if status_val not in {"staged", "accepted", "rejected", "expired"}:
        status_val = "staged"
    return CandidateSource(
        candidate_id=str(row["candidate_id"]),
        project_id=str(row["project_id"]),
        cite_key=str(row["cite_key"]),
        title=str(row["title"]),
        authors=authors,
        year=year_val,
        doi=str(row["doi"]) if row["doi"] else None,
        abstract=str(row["abstract"]) if row["abstract"] else None,
        source_api=str(row["source_api"]),
        bibtex=str(row["bibtex"]),
        grounding_tier=cast(GroundingTier, tier),
        pdf_path=str(row["pdf_path"]) if row["pdf_path"] else None,
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        status=cast(CandidateStatus, status_val),
    )


class CandidateStagingRepository:
    """SQLite lifecycle repository for AI-discovered candidate sources with DOI deduplication and TTL."""

    def __init__(self, db: DatabaseManager, ttl_days: int = 14) -> None:
        self.db = db
        self.ttl_days = max(1, int(ttl_days))

    def stage_candidate(
        self,
        project_id: str,
        cite_key: str,
        title: str,
        authors: list[str],
        year: int | None,
        doi: str | None,
        abstract: str | None,
        source_api: str,
        bibtex: str,
        grounding_tier: GroundingTier = "abstract_only",
        pdf_path: str | None = None,
        existing_bib_entries: dict[str, BibEntry] | None = None,
        now: datetime | None = None,
    ) -> CandidateSource:
        norm_doi = normalize_doi(doi)
        bib_map = existing_bib_entries or {}

        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM candidate_sources
                    WHERE project_id = ? AND status IN ('staged', 'accepted')
                    ORDER BY created_at ASC, rowid ASC;
                    """,
                    (project_id,),
                ).fetchall()
                existing_candidates = [_row_to_candidate(r) for r in rows]

                # 1. Check if DOI already exists among active/accepted staged candidates
                if norm_doi:
                    for cand in existing_candidates:
                        if normalize_doi(cand.doi) == norm_doi:
                            conn.execute("COMMIT;")
                            return cand

                # 2. Check if cite_key collides with existing_bib_entries or active candidates
                # If DOI matches an existing bib entry with the same cite_key, allow that key;
                # otherwise disambiguate with 'a', 'b', ... suffix.
                taken_keys: set[str] = {c.cite_key for c in existing_candidates}
                for b_key, b_entry in bib_map.items():
                    if norm_doi and normalize_doi(b_entry.doi) == norm_doi and b_key == cite_key:
                        continue
                    taken_keys.add(b_key)

                resolved_key = disambiguate_cite_key(cite_key, taken_keys)
                resolved_bibtex = rewrite_bibtex_cite_key(bibtex, resolved_key)

                created_dt = now if now is not None else datetime.now(timezone.utc)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                expires_dt = created_dt + timedelta(days=self.ttl_days)

                candidate_id = f"cand_{pysecrets.token_hex(8)}"
                clean_doi = norm_doi
                created_iso = created_dt.isoformat()
                expires_iso = expires_dt.isoformat()

                conn.execute(
                    """
                    INSERT INTO candidate_sources (
                        candidate_id, project_id, cite_key, title, authors_json, year,
                        doi, abstract, source_api, bibtex, grounding_tier, pdf_path,
                        created_at, expires_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged');
                    """,
                    (
                        candidate_id,
                        project_id,
                        resolved_key,
                        title.strip(),
                        json.dumps(list(authors)),
                        year,
                        clean_doi,
                        abstract,
                        source_api,
                        resolved_bibtex,
                        grounding_tier,
                        pdf_path,
                        created_iso,
                        expires_iso,
                    ),
                )
                conn.execute("COMMIT;")
                return CandidateSource(
                    candidate_id=candidate_id,
                    project_id=project_id,
                    cite_key=resolved_key,
                    title=title.strip(),
                    authors=list(authors),
                    year=year,
                    doi=clean_doi,
                    abstract=abstract,
                    source_api=source_api,
                    bibtex=resolved_bibtex,
                    grounding_tier=grounding_tier,
                    pdf_path=pdf_path,
                    created_at=created_iso,
                    expires_at=expires_iso,
                    status="staged",
                )
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise

    def list_active_candidates(self, project_id: str) -> list[CandidateSource]:
        with closing(self.db.get_connection()) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM candidate_sources
                WHERE project_id = ? AND status = 'staged'
                ORDER BY created_at ASC, rowid ASC;
                """,
                (project_id,),
            ).fetchall()
        return [_row_to_candidate(r) for r in rows]

    def get_candidate(
        self, project_id: str, candidate_id: str
    ) -> CandidateSource | None:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM candidate_sources
                WHERE project_id = ? AND candidate_id = ?;
                """,
                (project_id, candidate_id),
            ).fetchone()
        return _row_to_candidate(row) if row is not None else None

    def get_by_cite_key(
        self, project_id: str, cite_key: str
    ) -> CandidateSource | None:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM candidate_sources
                WHERE project_id = ? AND cite_key = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1;
                """,
                (project_id, cite_key),
            ).fetchone()
        return _row_to_candidate(row) if row is not None else None

    def expire_stale_candidates(self, now: datetime | None = None) -> int:
        ref_dt = now if now is not None else datetime.now(timezone.utc)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        ref_iso = ref_dt.isoformat()

        with closing(self.db.get_connection()) as conn:
            cursor = conn.execute(
                """
                UPDATE candidate_sources
                SET status = 'expired'
                WHERE status = 'staged' AND expires_at <= ?;
                """,
                (ref_iso,),
            )
            return int(cursor.rowcount or 0)

    def mark_status(
        self,
        project_id: str,
        cite_key: str,
        status: Literal["accepted", "rejected"],
    ) -> CandidateSource | None:
        if status not in {"accepted", "rejected"}:
            raise ValueError(f"Invalid status transition: {status!r}")
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                allowed_prior = "('staged')" if status == "rejected" else "('staged', 'accepted')"
                conn.execute(
                    f"""
                    UPDATE candidate_sources
                    SET status = ?
                    WHERE project_id = ? AND cite_key = ? AND status IN {allowed_prior};
                    """,
                    (status, project_id, cite_key),
                )
                row = conn.execute(
                    """
                    SELECT *
                    FROM candidate_sources
                    WHERE project_id = ? AND cite_key = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT 1;
                    """,
                    (project_id, cite_key),
                ).fetchone()
                conn.execute("COMMIT;")
                return _row_to_candidate(row) if row is not None else None
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise

    def update_cite_key_and_status(
        self,
        project_id: str,
        candidate_id: str,
        new_cite_key: str,
        new_bibtex: str,
        status: CandidateStatus,
    ) -> CandidateSource | None:
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                """
                UPDATE candidate_sources
                SET cite_key = ?, bibtex = ?, status = ?
                WHERE project_id = ? AND candidate_id = ?;
                """,
                (new_cite_key, new_bibtex, status, project_id, candidate_id),
            )
        return self.get_candidate(project_id, candidate_id)

    def attach_pdf(
        self,
        project_id: str,
        candidate_id: str,
        pdf_path: str,
    ) -> CandidateSource:
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                """
                UPDATE candidate_sources
                SET pdf_path = ?, grounding_tier = 'full_text_pdf'
                WHERE project_id = ? AND candidate_id = ?;
                """,
                (pdf_path, project_id, candidate_id),
            )
        updated = self.get_candidate(project_id, candidate_id)
        if updated is None:
            raise KeyError(f"Candidate not found: {candidate_id}")
        return updated
