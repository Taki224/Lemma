from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import bootstrap_data_dir
from app.db import DatabaseManager
from app.grounding.citation_verifier import parse_bib_file
from app.zotero.staging_repo import CandidateSource, CandidateStagingRepository


def _create_repo(tmp_path: Path, ttl_days: int = 14) -> CandidateStagingRepository:
    settings = bootstrap_data_dir(tmp_path / "data")
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    return CandidateStagingRepository(db=db, ttl_days=ttl_days)


def test_stage_candidate_deduplicates_doi_and_resolves_citekey_collision(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path, ttl_days=14)
    existing_bib = parse_bib_file(
        """@article{jones2023existing,
  author = {Jones, Alice},
  title = {Existing Paper in Bib},
  year = {2023},
  doi = {10.1000/jones2023}
}
"""
    )

    # 1. Stage initial candidate smith2024
    c1: CandidateSource = repo.stage_candidate(
        project_id="proj-1",
        cite_key="smith2024",
        title="Neural Theorem Proving in LaTeX",
        authors=["Smith, John", "Doe, Jane"],
        year=2024,
        doi="10.1234/smith.2024.001",
        abstract="We study neural theorem proving.",
        source_api="semantic_scholar",
        bibtex="@article{smith2024,\n  author={Smith, John and Doe, Jane},\n  title={Neural Theorem Proving in LaTeX},\n  year={2024},\n  doi={10.1234/smith.2024.001}\n}",
        grounding_tier="abstract_only",
        existing_bib_entries=existing_bib,
    )
    assert c1.cite_key == "smith2024"
    assert c1.status == "staged"
    assert c1.grounding_tier == "abstract_only"

    # 2. Staging the exact same DOI (even with a different proposed key or casing) returns the existing candidate
    c1_dup = repo.stage_candidate(
        project_id="proj-1",
        cite_key="smith2024duplicate",
        title="Neural Theorem Proving in LaTeX",
        authors=["Smith, John", "Doe, Jane"],
        year=2024,
        doi="https://doi.org/10.1234/SMITH.2024.001",
        abstract="We study neural theorem proving.",
        source_api="crossref",
        bibtex="@article{smith2024duplicate,\n  title={Neural Theorem Proving in LaTeX}\n}",
        grounding_tier="abstract_only",
        existing_bib_entries=existing_bib,
    )
    assert c1_dup.candidate_id == c1.candidate_id
    assert c1_dup.cite_key == "smith2024"

    # 3. Staging a DIFFERENT paper (different DOI) that collides on cite_key "smith2024" gets disambiguated as "smith2024a"
    c2 = repo.stage_candidate(
        project_id="proj-1",
        cite_key="smith2024",
        title="A Second Paper by Smith in 2024",
        authors=["Smith, Robert"],
        year=2024,
        doi="10.1234/smith.2024.002",
        abstract="Another distinct paper.",
        source_api="crossref",
        bibtex="@article{smith2024,\n  author={Smith, Robert},\n  title={A Second Paper by Smith in 2024},\n  year={2024},\n  doi={10.1234/smith.2024.002}\n}",
        grounding_tier="abstract_only",
        existing_bib_entries=existing_bib,
    )
    assert c2.candidate_id != c1.candidate_id
    assert c2.cite_key == "smith2024a"
    assert "@article{smith2024a," in c2.bibtex

    # 4. Staging a paper whose proposed cite_key collides with existing references.bib ("jones2023existing")
    # with a different DOI gets disambiguated as "jones2023existinga"
    c3 = repo.stage_candidate(
        project_id="proj-1",
        cite_key="jones2023existing",
        title="Different Jones Paper",
        authors=["Jones, Bob"],
        year=2023,
        doi="10.9999/different.jones",
        abstract="Different paper.",
        source_api="arxiv",
        bibtex="@article{jones2023existing,\n  title={Different Jones Paper}\n}",
        grounding_tier="abstract_only",
        existing_bib_entries=existing_bib,
    )
    assert c3.cite_key == "jones2023existinga"

    active = repo.list_active_candidates("proj-1")
    assert [c.cite_key for c in active] == [
        "smith2024",
        "smith2024a",
        "jones2023existinga",
    ]


def test_expire_stale_candidates_marks_unused_items_after_ttl(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path, ttl_days=14)
    base_time = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    fresh = repo.stage_candidate(
        project_id="proj-ttl",
        cite_key="fresh2026",
        title="Fresh Candidate",
        authors=["Author, A."],
        year=2026,
        doi="10.1000/fresh",
        abstract="Fresh abstract.",
        source_api="semantic_scholar",
        bibtex="@article{fresh2026, title={Fresh Candidate}}",
        grounding_tier="abstract_only",
        now=base_time,
    )
    stale = repo.stage_candidate(
        project_id="proj-ttl",
        cite_key="stale2026",
        title="Stale Candidate",
        authors=["Author, B."],
        year=2026,
        doi="10.1000/stale",
        abstract="Stale abstract.",
        source_api="semantic_scholar",
        bibtex="@article{stale2026, title={Stale Candidate}}",
        grounding_tier="abstract_only",
        now=base_time - timedelta(days=15),
    )

    # Expire at base_time: stale (created 15 days ago with 14-day TTL) must expire, fresh remains staged
    expired_count = repo.expire_stale_candidates(now=base_time)
    assert expired_count == 1

    active = repo.list_active_candidates("proj-ttl")
    assert [c.cite_key for c in active] == [fresh.cite_key]

    stale_reloaded = repo.get_candidate("proj-ttl", stale.candidate_id)
    assert stale_reloaded is not None
    assert stale_reloaded.status == "expired"
