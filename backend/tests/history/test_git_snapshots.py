from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.config import bootstrap_data_dir
from app.db import DatabaseManager
from app.grounding.citation_verifier import parse_bib_file
from app.history.git_snapshots import GitSnapshotService, SnapshotInfo
from app.projects.service import ProjectService


def _create_project_service(tmp_path: Path) -> tuple[ProjectService, Any]:
    settings = bootstrap_data_dir(tmp_path / "data")
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    return ProjectService(data_dir=settings.data_dir, db=db), settings


def test_restore_returns_byte_identical_sources_including_figure_pdfs_and_creates_pre_restore_snapshot(
    tmp_path: Path,
) -> None:
    project_service, settings = _create_project_service(tmp_path)
    project = project_service.create_project(name="Snapshot Test Project")
    project_dir = project_service.get_project_dir(project.project_id)

    service = GitSnapshotService()
    compile_lock = asyncio.Lock()

    # Step 1: Create initial files (including figures/plot.pdf)
    main_tex = project_dir / "main.tex"
    main_tex.write_text("\\documentclass{article}\n\\begin{document}\nInitial content\n\\end{document}\n")
    fig_dir = project_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    plot_pdf = fig_dir / "plot.pdf"
    pdf_bytes_v1 = b"%PDF-1.4 binary figure content version 1 \x00\xff\xfe"
    plot_pdf.write_bytes(pdf_bytes_v1)

    snap1 = service.create_snapshot(project_dir, trigger="compile_success", message="Initial compile")
    assert snap1 is not None
    assert snap1.commit_sha
    assert snap1.trigger == "compile_success"

    # Step 2: Create snapshot 2 with modified tex and new figure bytes
    main_tex.write_text("\\documentclass{article}\n\\begin{document}\nVersion 2 content\n\\end{document}\n")
    pdf_bytes_v2 = b"%PDF-1.4 binary figure content version 2 \x12\x34\x56"
    plot_pdf.write_bytes(pdf_bytes_v2)

    snap2 = service.create_snapshot(project_dir, trigger="compile_success", message="Version 2 compile")
    assert snap2 is not None
    assert snap2.commit_sha != snap1.commit_sha

    # Step 3: Modify working directory and prepare unsaved buffer
    main_tex.write_text("Dirty uncommitted edit")
    unsaved_buffers = {
        "notes.tex": "Unsaved notes content",
    }

    # Step 3b: Verify path traversal attempt in unsaved_buffers is rejected
    traversal_buffers = {
        "../evil.tex": "attack",
    }
    with pytest.raises((ValueError, PermissionError)):
        asyncio.run(
            service.restore_snapshot(
                project_id=project.project_id,
                project_dir=project_dir,
                target_sha=snap1.commit_sha,
                project_service=project_service,
                compile_lock=compile_lock,
                unsaved_buffers=traversal_buffers,
            )
        )

    # Step 4: Restore snapshot 1
    restore_info = asyncio.run(
        service.restore_snapshot(
            project_id=project.project_id,
            project_dir=project_dir,
            target_sha=snap1.commit_sha,
            project_service=project_service,
            compile_lock=compile_lock,
            unsaved_buffers=unsaved_buffers,
        )
    )
    assert restore_info is not None

    # Verify pre_restore commit was created and recorded
    snapshots = service.list_snapshots(project_dir)
    triggers = [s.trigger for s in snapshots]
    assert "pre_restore" in triggers

    # Verify byte-for-byte identity of restored files
    assert main_tex.read_text() == "\\documentclass{article}\n\\begin{document}\nInitial content\n\\end{document}\n"
    assert plot_pdf.read_bytes() == pdf_bytes_v1


def test_preview_restore_warns_when_references_bib_would_lose_entries(
    tmp_path: Path,
) -> None:
    project_service, settings = _create_project_service(tmp_path)
    project = project_service.create_project(name="Bib Loss Project")
    project_dir = project_service.get_project_dir(project.project_id)

    service = GitSnapshotService()

    # Snapshot 1: references.bib has vaswani2017
    bib_file = project_dir / "references.bib"
    bib_file.write_text(
        "@article{vaswani2017,\n  title={Attention is all you need},\n  author={Vaswani, Ashish},\n  year={2017}\n}\n"
    )
    snap1 = service.create_snapshot(project_dir, trigger="compile_success", message="Snapshot 1")
    assert snap1 is not None

    # Snapshot 2: references.bib adds brown2020
    bib_file.write_text(
        "@article{vaswani2017,\n  title={Attention is all you need},\n  author={Vaswani, Ashish},\n  year={2017}\n}\n"
        "@article{brown2020,\n  title={Language models are few-shot learners},\n  author={Brown, Tom},\n  year={2020}\n}\n"
    )
    snap2 = service.create_snapshot(project_dir, trigger="compile_success", message="Snapshot 2")
    assert snap2 is not None

    # Preview restore to snapshot 1
    preview = service.preview_restore(project_dir=project_dir, target_sha=snap1.commit_sha)
    assert preview.commit_sha == snap1.commit_sha
    assert preview.has_bib_loss_warning is True
    assert "brown2020" in preview.lost_bib_keys
    assert "references.bib" in preview.changed_files
    assert len(preview.unified_diff) > 0


def test_git_hooks_and_filters_disabled_paper_reviews_excluded_and_referenced_shas_pinned_on_compact(
    tmp_path: Path,
) -> None:
    project_service, settings = _create_project_service(tmp_path)
    project = project_service.create_project(name="Security & Compaction Project")
    project_dir = project_service.get_project_dir(project.project_id)

    service = GitSnapshotService()
    compile_lock = asyncio.Lock()

    # Step 1: Create initial snapshot
    doc = project_dir / "main.tex"
    doc.write_text("Hello world")
    snap1 = service.create_snapshot(project_dir, trigger="compile_success", message="Initial")
    assert snap1 is not None

    # Step 2: Inject a malicious executable pre-commit hook into .git/hooks
    hooks_dir = project_dir / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    poisoned_hook = hooks_dir / "pre-commit"
    marker_file = tmp_path / "hook_ran.marker"
    poisoned_hook.write_text(f"#!/bin/sh\ntouch '{marker_file}'\nexit 1\n")
    poisoned_hook.chmod(0o777)

    # Step 3: Create snapshot 2 — should succeed because core.hooksPath=/dev/null
    doc.write_text("Hello world 2")
    snap2 = service.create_snapshot(project_dir, trigger="compile_success", message="Commit with hook present")
    assert snap2 is not None
    assert not marker_file.exists()  # Hook must NOT have executed

    # Step 4: Put a review in paper-reviews/ referencing snap1
    reviews_dir = project_dir / "paper-reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    review_file = reviews_dir / "review_01.md"
    review_file.write_text(f"# Review\nAudited commit: {snap1.commit_sha}\nLooks good.")

    # Restore to snap2 and verify paper-reviews/ is untouched
    asyncio.run(
        service.restore_snapshot(
            project_id=project.project_id,
            project_dir=project_dir,
            target_sha=snap2.commit_sha,
            project_service=project_service,
            compile_lock=compile_lock,
        )
    )
    assert review_file.exists()
    assert snap1.commit_sha in review_file.read_text()

    # Step 5: Test compaction preserves snap1 because it is pinned by paper-reviews/
    # Create several intermediate compile_success snapshots
    for i in range(5):
        doc.write_text(f"Hello world loop {i}")
        service.create_snapshot(project_dir, trigger="compile_success", message=f"Loop {i}")

    # Compact keeping only 2 recent snapshots
    compacted_count = service.compact_old_snapshots(project_dir=project_dir, keep_recent=2)
    assert compacted_count >= 1

    remaining = service.list_snapshots(project_dir)
    remaining_shas = {s.commit_sha for s in remaining}
    # snap1 must still be present because it is pinned in paper-reviews!
    assert snap1.commit_sha in remaining_shas
