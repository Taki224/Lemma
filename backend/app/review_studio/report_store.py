from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.review_studio.text_review import ReviewIssue, Severity


@dataclass
class ResolvedReviewIssue:
    issue_id: str
    reviewer_persona: str
    model: str
    severity: Severity
    file_path: str
    original_line: int | None
    resolved_line: int | None
    page_number: int | None
    quoted_anchor: str
    title: str
    critique: str
    suggested_fix: str | None = None
    stale: bool = False


@dataclass
class ReviewReport:
    report_id: str
    timestamp: str
    snapshot_sha: str
    issues: list[ResolvedReviewIssue]


_METADATA_REGEX = re.compile(r"<!--\s*METADATA:\s*(\{.*?\})\s*-->", re.DOTALL)


class ReviewReportStore:
    """Persistent storage for peer review reports in paper-reviews/ with quoted-anchor re-resolution."""

    def save_report(
        self,
        project_dir: Path,
        snapshot_sha: str,
        issues: list[ReviewIssue],
    ) -> Path:
        """Write structured Markdown + JSON metadata into paper-reviews/review-<timestamp>.md."""
        reviews_dir = project_dir / "paper-reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)

        now_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report_id = f"review-{now_str}"
        report_file = reviews_dir / f"{report_id}.md"

        serializable_issues: list[dict[str, Any]] = []
        for iss in issues:
            # Validate path relative to project
            sanitized_path = ""
            if iss.file_path:
                try:
                    resolved = (project_dir / iss.file_path).resolve()
                    if resolved.is_relative_to(project_dir.resolve()) and not iss.file_path.startswith(".."):
                        sanitized_path = resolved.relative_to(project_dir.resolve()).as_posix()
                except (OSError, ValueError):
                    sanitized_path = ""

            serializable_issues.append(
                {
                    "issue_id": iss.issue_id,
                    "reviewer_persona": iss.reviewer_persona,
                    "model": iss.model,
                    "severity": iss.severity,
                    "file_path": sanitized_path,
                    "line_number": iss.line_number,
                    "page_number": iss.page_number,
                    "quoted_anchor": iss.quoted_anchor,
                    "title": iss.title,
                    "critique": iss.critique,
                    "suggested_fix": iss.suggested_fix,
                }
            )

        metadata_dict = {
            "report_id": report_id,
            "snapshot_sha": snapshot_sha,
            "timestamp": now_str,
            "issues": serializable_issues,
        }

        # Build clean Markdown document
        md_lines = [
            f"<!-- METADATA: {json.dumps(metadata_dict, indent=2)} -->",
            "",
            f"# Academic Peer Review Report — `{report_id}`",
            "",
            f"- **Snapshot Commit**: `{snapshot_sha}`",
            f"- **Timestamp**: `{now_str}`",
            f"- **Total Issues**: {len(serializable_issues)}",
            "",
            "## Review Findings",
            "",
        ]

        for iss_data in serializable_issues:
            sev_badge = iss_data["severity"].upper()
            title = iss_data["title"]
            persona = iss_data["reviewer_persona"]
            model = iss_data["model"]
            file_p = iss_data["file_path"]
            line = iss_data["line_number"]
            page = iss_data["page_number"]
            anchor = iss_data["quoted_anchor"]
            critique = iss_data["critique"]
            fix = iss_data["suggested_fix"]

            loc_str = ""
            if file_p:
                loc_str += f"`{file_p}`"
                if line is not None:
                    loc_str += f":line {line}"
            if page is not None:
                if loc_str:
                    loc_str += f" (page {page})"
                else:
                    loc_str = f"Page {page}"

            md_lines.append(f"### [{sev_badge}] {title}")
            md_lines.append(f"*Reviewer*: {persona} ({model}) | *Location*: {loc_str or 'General'}")
            md_lines.append("")
            if anchor:
                md_lines.append(f"> \"{anchor}\"")
                md_lines.append("")
            md_lines.append(critique)
            md_lines.append("")
            if fix:
                md_lines.append(f"**Suggested Fix**: {fix}")
                md_lines.append("")

        report_file.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        return report_file

    def resolve_issue_locations(
        self,
        project_dir: Path,
        report_id: str,
    ) -> list[ResolvedReviewIssue]:
        """Load report and re-resolve issue line numbers against current project files."""
        if not report_id or "/" in report_id or "\\" in report_id or ".." in report_id:
            return []

        reviews_dir = (project_dir / "paper-reviews").resolve()
        candidates = [
            reviews_dir / f"{report_id}.md",
            reviews_dir / report_id,
        ]

        target_file: Path | None = None
        for cand in candidates:
            if cand.resolve().is_relative_to(reviews_dir) and cand.is_file():
                target_file = cand
                break

        if target_file is None:
            return []

        try:
            content = target_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        match = _METADATA_REGEX.search(content)
        if not match:
            return []

        try:
            metadata = json.loads(match.group(1))
            issues_data = metadata.get("issues", [])
        except Exception:
            return []

        resolved: list[ResolvedReviewIssue] = []
        for raw in issues_data:
            orig_line = raw.get("line_number")
            anchor = str(raw.get("quoted_anchor") or "").strip()
            fpath = str(raw.get("file_path") or "").strip()

            resolved_line = orig_line
            stale = False

            if fpath and anchor:
                try:
                    target_source = (project_dir / fpath).resolve()
                    if (
                        not target_source.is_relative_to(project_dir.resolve())
                        or not target_source.is_file()
                    ):
                        resolved_line = orig_line
                        stale = True
                    else:
                        file_lines = target_source.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()
                        num_lines = len(file_lines)

                        line_matched = False
                        if orig_line is not None and 1 <= orig_line <= num_lines:
                            if anchor in file_lines[orig_line - 1]:
                                resolved_line = orig_line
                                line_matched = True
                                stale = False

                        if not line_matched:
                            for idx, line_text in enumerate(file_lines, start=1):
                                stripped_text = line_text.strip()
                                if anchor in line_text or (
                                    len(stripped_text) >= 8 and stripped_text in anchor
                                ):
                                    resolved_line = idx
                                    line_matched = True
                                    stale = False
                                    break

                        if not line_matched:
                            resolved_line = orig_line
                            stale = True
                except OSError:
                    resolved_line = orig_line
                    stale = True
            elif fpath and not anchor:
                resolved_line = orig_line
                stale = False
            else:
                # e.g. visual layout issues
                resolved_line = orig_line
                stale = False

            resolved.append(
                ResolvedReviewIssue(
                    issue_id=str(raw.get("issue_id") or ""),
                    reviewer_persona=str(raw.get("reviewer_persona") or ""),
                    model=str(raw.get("model") or ""),
                    severity=raw.get("severity", "minor"),
                    file_path=fpath,
                    original_line=orig_line,
                    resolved_line=resolved_line,
                    page_number=raw.get("page_number"),
                    quoted_anchor=anchor,
                    title=str(raw.get("title") or ""),
                    critique=str(raw.get("critique") or ""),
                    suggested_fix=raw.get("suggested_fix"),
                    stale=stale,
                )
            )

        return resolved

    def list_reports(self, project_dir: Path) -> list[dict[str, Any]]:
        """List summary of saved reports in paper-reviews/."""
        reviews_dir = project_dir / "paper-reviews"
        if not reviews_dir.is_dir():
            return []

        results: list[dict[str, Any]] = []
        for report_file in sorted(reviews_dir.glob("review-*.md"), reverse=True):
            try:
                content = report_file.read_text(encoding="utf-8", errors="replace")
                match = _METADATA_REGEX.search(content)
                if match:
                    meta = json.loads(match.group(1))
                    results.append(
                        {
                            "report_id": meta.get("report_id", report_file.stem),
                            "snapshot_sha": meta.get("snapshot_sha", ""),
                            "timestamp": meta.get("timestamp", ""),
                            "issue_count": len(meta.get("issues", [])),
                        }
                    )
                else:
                    results.append(
                        {
                            "report_id": report_file.stem,
                            "snapshot_sha": "",
                            "timestamp": "",
                            "issue_count": 0,
                        }
                    )
            except Exception:
                continue

        return results
