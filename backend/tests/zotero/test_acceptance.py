from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import bootstrap_data_dir
from app.db import DatabaseManager
from app.grounding.citation_verifier import parse_bib_file
from app.projects.service import ProjectService
from app.zotero.acceptance import CitationAcceptanceCoordinator
from app.zotero.client import ZoteroClient
from app.zotero.staging_repo import CandidateStagingRepository


@pytest.mark.asyncio
async def test_rejecting_diff_never_modifies_references_bib_or_calls_zotero(
    tmp_path: Path,
) -> None:
    settings = bootstrap_data_dir(tmp_path / "data")
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    project_service = ProjectService(data_dir=settings.data_dir, db=db)
    meta = project_service.create_project("Rejection Safety Test")
    project_dir = project_service.get_project_dir(meta.project_id)

    initial_bib = project_service.read_file(meta.project_id, "references.bib").content
    repo = CandidateStagingRepository(db=db)

    repo.stage_candidate(
        project_id=meta.project_id,
        cite_key="cand2024one",
        title="Candidate Paper One",
        authors=["Alpha, Alice"],
        year=2024,
        doi="10.1000/cand.one",
        abstract="Abstract one.",
        source_api="semantic_scholar",
        bibtex="@article{cand2024one,\n  author = {Alpha, Alice},\n  title = {Candidate Paper One},\n  year = {2024}\n}",
    )
    repo.stage_candidate(
        project_id=meta.project_id,
        cite_key="cand2024two",
        title="Candidate Paper Two",
        authors=["Beta, Bob"],
        year=2024,
        doi="10.1000/cand.two",
        abstract="Abstract two.",
        source_api="crossref",
        bibtex="@article{cand2024two,\n  author = {Beta, Bob},\n  title = {Candidate Paper Two},\n  year = {2024}\n}",
    )

    zotero_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        zotero_requests.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json=[])

    zotero_client = ZoteroClient(
        api_key="rw-key",
        library_id="12345",
        library_type="user",
        transport=httpx.MockTransport(handler),
    )

    snapshot_calls: list[tuple[Path, str]] = []

    def fake_snapshot_hook(p_dir: Path, label: str) -> None:
        snapshot_calls.append((p_dir, label))

    coordinator = CitationAcceptanceCoordinator(
        staging_repo=repo,
        on_ai_accept=fake_snapshot_hook,
    )

    result = await coordinator.accept_hunk_citations(
        project_id=meta.project_id,
        project_dir=project_dir,
        project_service=project_service,
        accepted_cite_keys=[],
        rejected_cite_keys=["cand2024one", "cand2024two"],
        zotero_client=zotero_client,
        collection_key="COLL01",
        push_enabled=True,
    )

    assert result.accepted_cite_keys == []
    assert result.rejected_cite_keys == ["cand2024one", "cand2024two"]
    assert result.key_remapping == {}
    assert result.zotero_pushed_keys == []

    # 1. Verify references.bib is byte-for-byte unchanged
    after_bib = project_service.read_file(meta.project_id, "references.bib").content
    assert after_bib == initial_bib

    # 2. Verify zero Zotero HTTP calls occurred on rejection
    assert len(zotero_requests) == 0

    # 3. Verify snapshot hook is not triggered when nothing was accepted
    assert len(snapshot_calls) == 0

    # 4. Verify both candidates are marked rejected and no longer in active list
    assert repo.list_active_candidates(meta.project_id) == []
    c1 = repo.get_by_cite_key(meta.project_id, "cand2024one")
    c2 = repo.get_by_cite_key(meta.project_id, "cand2024two")
    assert c1 is not None and c1.status == "rejected"
    assert c2 is not None and c2.status == "rejected"


@pytest.mark.asyncio
async def test_partial_per_cite_acceptance_and_collision_key_remapping(
    tmp_path: Path,
) -> None:
    settings = bootstrap_data_dir(tmp_path / "data")
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    project_service = ProjectService(data_dir=settings.data_dir, db=db)
    meta = project_service.create_project("Partial Cite Acceptance Test")
    project_dir = project_service.get_project_dir(meta.project_id)

    repo = CandidateStagingRepository(db=db)

    # Stage citeA ("smith2024") and citeB ("jones2025") before references.bib gets a concurrent "smith2024"
    repo.stage_candidate(
        project_id=meta.project_id,
        cite_key="smith2024",
        title="Staged Smith Paper on Grounded LaTeX",
        authors=["Smith, Clara"],
        year=2024,
        doi="10.5555/staged.smith.2024",
        abstract="Staged Smith abstract.",
        source_api="semantic_scholar",
        bibtex="@article{smith2024,\n  author = {Smith, Clara},\n  title = {Staged Smith Paper on Grounded LaTeX},\n  year = {2024},\n  doi = {10.5555/staged.smith.2024}\n}",
    )
    repo.stage_candidate(
        project_id=meta.project_id,
        cite_key="jones2025",
        title="Rejected Jones Paper",
        authors=["Jones, Derek"],
        year=2025,
        doi="10.5555/rejected.jones.2025",
        abstract="Rejected Jones abstract.",
        source_api="crossref",
        bibtex="@article{jones2025,\n  author = {Jones, Derek},\n  title = {Rejected Jones Paper},\n  year = {2025},\n  doi = {10.5555/rejected.jones.2025}\n}",
    )

    # Simulate user adding a different "smith2024" to references.bib after candidate was staged
    existing_bib = """@article{smith2024,
  author = {Smith, Original},
  title = {User Added Smith Paper},
  year = {2024},
  doi = {10.1111/user.smith.2024}
}
"""
    project_service.write_file(
        project_id=meta.project_id,
        rel_path="references.bib",
        content=existing_bib,
        expected_etag=None,
    )

    pushed_payloads: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/keys/"):
            return httpx.Response(
                200,
                json={
                    "key": "rw-key",
                    "userID": 12345,
                    "access": {"user": {"library": True, "write": True}},
                },
            )
        if request.url.path.endswith("/collections"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path.endswith("/items"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/items"):
            pushed_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"successful": {"0": {"key": "ZOT_SMITH_2024A"}}},
            )
        return httpx.Response(200, json=[])

    zotero_client = ZoteroClient(
        api_key="rw-key",
        library_id="12345",
        library_type="user",
        transport=httpx.MockTransport(handler),
    )

    snapshot_calls: list[tuple[Path, str]] = []
    coordinator = CitationAcceptanceCoordinator(
        staging_repo=repo,
        on_ai_accept=lambda p_dir, label: snapshot_calls.append((p_dir, label)),
    )

    # Track ETag-checked write call
    orig_write = project_service.write_file
    recorded_expected_etags: list[str | None] = []

    def spy_write_file(
        project_id: str,
        rel_path: str,
        content: bytes | str,
        expected_etag: str | None = None,
    ):
        recorded_expected_etags.append(expected_etag)
        return orig_write(project_id, rel_path, content, expected_etag=expected_etag)

    project_service.write_file = spy_write_file  # type: ignore[method-assign]
    try:
        result = await coordinator.accept_hunk_citations(
            project_id=meta.project_id,
            project_dir=project_dir,
            project_service=project_service,
            accepted_cite_keys=["smith2024"],
            rejected_cite_keys=["jones2025"],
            zotero_client=zotero_client,
            collection_key="COLL01",
            push_enabled=True,
        )
    finally:
        project_service.write_file = orig_write  # type: ignore[method-assign]

    # 1. Last-moment collision resolved: smith2024 -> smith2024a
    assert result.key_remapping == {"smith2024": "smith2024a"}
    assert result.accepted_cite_keys == ["smith2024a"]
    assert result.rejected_cite_keys == ["jones2025"]
    assert result.zotero_pushed_keys == ["ZOT_SMITH_2024A"]

    # 2. ETag verification was enforced on references.bib write
    assert len(recorded_expected_etags) == 1
    assert recorded_expected_etags[0] is not None

    # 3. Verify references.bib contains original smith2024 AND newly accepted smith2024a, but NOT jones2025
    updated_bib = project_service.read_file(meta.project_id, "references.bib").text
    entries = parse_bib_file(updated_bib)
    assert "smith2024" in entries
    assert entries["smith2024"].title == "User Added Smith Paper"
    assert "smith2024a" in entries
    assert entries["smith2024a"].title == "Staged Smith Paper on Grounded LaTeX"
    assert "jones2025" not in entries

    # 4. Verify only accepted item was pushed to Zotero
    assert len(pushed_payloads) == 1
    assert pushed_payloads[0][0]["title"] == "Staged Smith Paper on Grounded LaTeX"

    # 5. Verify snapshot hook ran once
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0][0] == project_dir
