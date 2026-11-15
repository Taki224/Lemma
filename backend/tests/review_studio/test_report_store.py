from __future__ import annotations

from pathlib import Path

import pytest

from app.review_studio.report_store import ResolvedReviewIssue, ReviewReportStore
from app.review_studio.text_review import ReviewIssue


def test_shifted_line_reresolves_by_quoted_anchor(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_shift"
    project_dir.mkdir(parents=True, exist_ok=True)
    tex_path = project_dir / "chapter1.tex"
    original_lines = [
        r"\section{Introduction}",
        r"This is the opening statement.",
        r"We prove that P does not equal NP under standard oracle models.",
        r"The rest of the paper continues.",
    ]
    tex_path.write_text("\n".join(original_lines) + "\n", encoding="utf-8")

    issue = ReviewIssue(
        issue_id="iss_1",
        reviewer_persona="methodology_critic",
        model="claude-3.7-sonnet",
        severity="major",
        file_path="chapter1.tex",
        line_number=3,
        page_number=1,
        quoted_anchor="We prove that P does not equal NP under standard oracle models.",
        title="Unproven complexity claim",
        critique="The oracle proof does not extend to the unrelativized setting.",
        suggested_fix=None,
    )

    store = ReviewReportStore()
    report_path = store.save_report(
        project_dir=project_dir,
        snapshot_sha="abc1234def",
        issues=[issue],
    )

    assert report_path.is_file()
    assert "abc1234def" in report_path.read_text(encoding="utf-8")

    # Shift lines by prepending 10 comments at the beginning of the file
    shifted_content = "\n".join([f"% comment {i}" for i in range(10)]) + "\n" + "\n".join(original_lines) + "\n"
    tex_path.write_text(shifted_content, encoding="utf-8")

    resolved_issues = store.resolve_issue_locations(project_dir=project_dir, report_id=report_path.stem)

    assert len(resolved_issues) == 1
    res = resolved_issues[0]
    assert isinstance(res, ResolvedReviewIssue)
    assert res.stale is False
    # Original line was 3; after adding 10 lines at the top, it should resolve to line 13
    assert res.original_line == 3
    assert res.resolved_line == 13


def test_deleted_anchor_marks_issue_as_stale(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_stale"
    project_dir.mkdir(parents=True, exist_ok=True)
    tex_path = project_dir / "main.tex"
    tex_path.write_text(
        r"\documentclass{article}" + "\n"
        r"\begin{document}" + "\n"
        r"A remarkable discovery was made in 2026." + "\n"
        r"\end{document}" + "\n",
        encoding="utf-8",
    )

    issue = ReviewIssue(
        issue_id="iss_2",
        reviewer_persona="literature_auditor",
        model="gemini-2.5-pro",
        severity="minor",
        file_path="main.tex",
        line_number=3,
        page_number=1,
        quoted_anchor="A remarkable discovery was made in 2026.",
        title="Missing citation for discovery",
        critique="Needs citation to original preprint.",
    )

    store = ReviewReportStore()
    report_path = store.save_report(
        project_dir=project_dir,
        snapshot_sha="sha999",
        issues=[issue],
    )

    # Completely delete the quoted anchor line
    tex_path.write_text(
        r"\documentclass{article}" + "\n"
        r"\begin{document}" + "\n"
        r"Completely rewritten text with no matching anchor." + "\n"
        r"\end{document}" + "\n",
        encoding="utf-8",
    )

    resolved = store.resolve_issue_locations(project_dir=project_dir, report_id=report_path.stem)
    assert len(resolved) == 1
    assert resolved[0].stale is True
    assert resolved[0].resolved_line == 3


def test_empty_lines_do_not_falsely_match_anchors(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_empty_lines"
    project_dir.mkdir(parents=True, exist_ok=True)
    tex_path = project_dir / "main.tex"
    # Content with multiple blank lines
    tex_path.write_text("\n\n\n\\section{Real Content}\n\n", encoding="utf-8")

    issue = ReviewIssue(
        issue_id="iss_empty",
        reviewer_persona="structure_editor",
        model="claude-3.7-sonnet",
        severity="nit",
        file_path="main.tex",
        line_number=4,
        page_number=1,
        quoted_anchor="Non-existent phrase that has plenty of length to test anchor search",
        title="Style nit",
        critique="Needs clarification.",
    )

    store = ReviewReportStore()
    report_path = store.save_report(
        project_dir=project_dir,
        snapshot_sha="sha_blank",
        issues=[issue],
    )

    resolved = store.resolve_issue_locations(project_dir=project_dir, report_id=report_path.stem)
    assert len(resolved) == 1
    # Must NOT match empty lines (e.g. line 1, 2, 3)
    assert resolved[0].stale is True
    assert resolved[0].resolved_line == 4


def test_resolve_issue_locations_rejects_path_traversal(tmp_path: Path) -> None:
    store = ReviewReportStore()
    resolved = store.resolve_issue_locations(project_dir=tmp_path, report_id="../../etc/passwd")
    assert resolved == []
