from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.grounding.citation_verifier import parse_bib_file
from app.projects.service import ProjectService

SnapshotTrigger = Literal["compile_success", "ai_accept", "pre_restore", "manual"]

_SAFE_GIT_ARGS = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.file.allow=never",
    "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.clean=",
    "-c", "core.fsmonitor=false",
]

_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Lemma",
    "GIT_AUTHOR_EMAIL": "editor@lemma.local",
    "GIT_COMMITTER_NAME": "Lemma",
    "GIT_COMMITTER_EMAIL": "editor@lemma.local",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"),
}

_DEFAULT_GITIGNORE = """# Lemma automatic gitignore
.build/
.texmf/
project-uploads/*.pdf
*.synctex.gz
paper-reviews/
secrets.yaml
*.key
*.pem
"""

_TRIGGER_RE = re.compile(r"^\[([a-zA-Z0-9_]+)\]\s*(.*)$")
_SHA_HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_VALID_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def validate_commit_sha(sha: str) -> str:
    clean = sha.strip()
    if not _VALID_SHA_RE.match(clean):
        raise ValueError(f"Invalid commit SHA format: {sha}")
    return clean.lower()


@dataclass(frozen=True)
class SnapshotInfo:
    commit_sha: str
    timestamp: str
    trigger: SnapshotTrigger
    message: str


@dataclass(frozen=True)
class RestorePreview:
    commit_sha: str
    changed_files: list[str]
    unified_diff: str
    lost_bib_keys: list[str]
    has_bib_loss_warning: bool


class GitSnapshotService:
    """Automatic Git version checkpointing with pre-restore snapshots and .bib loss detection."""

    def __init__(self) -> None:
        pass

    def _run_git(
        self,
        project_dir: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["git", *_SAFE_GIT_ARGS, *args]
        return subprocess.run(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            env=_GIT_ENV,
            check=check,
        )

    def ensure_repo(self, project_dir: Path) -> None:
        p_dir = Path(project_dir).resolve()
        git_dir = p_dir / ".git"
        if not git_dir.is_dir():
            self._run_git(p_dir, "init")
            gitignore_file = p_dir / ".gitignore"
            if not gitignore_file.exists():
                gitignore_file.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")
            self._run_git(p_dir, "add", ".gitignore")
            self._run_git(p_dir, "commit", "-m", "[manual] Initialized project repository")
        else:
            gitignore_file = p_dir / ".gitignore"
            if not gitignore_file.exists():
                gitignore_file.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")

        # Strip / neutralize any malicious alias or filter in .git/config
        cfg_file = git_dir / "config"
        if cfg_file.is_file():
            try:
                content = cfg_file.read_text(encoding="utf-8", errors="ignore")
                # Remove dangerous sections if present
                for dangerous in ("[alias]", "[filter", "[include", "[core]"):
                    if dangerous in content and dangerous != "[core]":
                        content = re.sub(rf"\[{dangerous[1:-1]}[^\]]*\][^\[]*", "", content)
                cfg_file.write_text(content, encoding="utf-8")
            except Exception:
                pass

    def create_snapshot(
        self,
        project_dir: Path,
        trigger: str = "manual",
        message: str = "Snapshot",
    ) -> SnapshotInfo | None:
        p_dir = Path(project_dir).resolve()
        self.ensure_repo(p_dir)

        # Stage tracked & untracked files respecting .gitignore
        self._run_git(p_dir, "add", "-A")

        # Check if working directory is clean
        diff_res = self._run_git(p_dir, "diff", "--cached", "--quiet", check=False)
        has_changes = diff_res.returncode != 0

        clean_trigger = trigger if trigger in {"compile_success", "ai_accept", "pre_restore", "manual"} else "manual"
        commit_msg = f"[{clean_trigger}] {message.strip()}"

        if not has_changes:
            # Nothing changed. If repo has commits, return latest snapshot
            rev_res = self._run_git(p_dir, "log", "-1", "--format=%H|%cI|%s", check=False)
            if rev_res.returncode == 0 and rev_res.stdout.strip():
                parts = rev_res.stdout.strip().split("|", 2)
                head_sha = parts[0]
                if clean_trigger == "pre_restore":
                    tag_name = f"pre_restore_{head_sha[:8]}_{int(time.time())}"
                    self._run_git(p_dir, "tag", "-f", "-a", "-m", f"Pre-restore checkpoint: {message}", tag_name, head_sha, check=False)
                return SnapshotInfo(
                    commit_sha=head_sha,
                    timestamp=parts[1] if len(parts) > 1 else datetime.now(timezone.utc).isoformat(),
                    trigger=clean_trigger,  # type: ignore[arg-type]
                    message=parts[2] if len(parts) > 2 else message,
                )
            return None

        self._run_git(p_dir, "commit", "-m", commit_msg)
        log_res = self._run_git(p_dir, "log", "-1", "--format=%H|%cI|%s")
        parts = log_res.stdout.strip().split("|", 2)
        sha = parts[0]
        iso_ts = parts[1] if len(parts) > 1 else datetime.now(timezone.utc).isoformat()

        if clean_trigger == "pre_restore":
            tag_name = f"pre_restore_{sha[:8]}_{int(time.time())}"
            self._run_git(p_dir, "tag", "-a", "-m", f"Pre-restore checkpoint: {message}", tag_name, sha, check=False)

        return SnapshotInfo(
            commit_sha=sha,
            timestamp=iso_ts,
            trigger=clean_trigger,  # type: ignore[arg-type]
            message=message,
        )

    def list_snapshots(self, project_dir: Path) -> list[SnapshotInfo]:
        p_dir = Path(project_dir).resolve()
        self.ensure_repo(p_dir)

        log_res = self._run_git(p_dir, "log", "--format=%H|%cI|%s", check=False)
        if log_res.returncode != 0 or not log_res.stdout.strip():
            return []

        snapshots: list[SnapshotInfo] = []
        for line in log_res.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            sha, ts, full_msg = parts[0], parts[1], parts[2]
            trigger: SnapshotTrigger = "manual"
            msg = full_msg
            m = _TRIGGER_RE.match(full_msg)
            if m:
                trig_str = m.group(1)
                if trig_str in {"compile_success", "ai_accept", "pre_restore", "manual"}:
                    trigger = trig_str  # type: ignore[assignment]
                msg = m.group(2)

            snapshots.append(
                SnapshotInfo(
                    commit_sha=sha,
                    timestamp=ts,
                    trigger=trigger,
                    message=msg,
                )
            )
        return snapshots

    def preview_restore(self, project_dir: Path, target_sha: str) -> RestorePreview:
        valid_sha = validate_commit_sha(target_sha)
        p_dir = Path(project_dir).resolve()
        self.ensure_repo(p_dir)

        # Unified diff: what changes going from current HEAD to target_sha
        diff_res = self._run_git(p_dir, "diff", "HEAD", valid_sha, check=False)
        unified_diff = diff_res.stdout

        names_res = self._run_git(p_dir, "diff", "--name-only", "HEAD", valid_sha, check=False)
        changed_files = [f.strip() for f in names_res.stdout.splitlines() if f.strip()]

        # Parse current references.bib keys
        current_bib_file = p_dir / "references.bib"
        current_keys: set[str] = set()
        if current_bib_file.is_file():
            raw_text = current_bib_file.read_text(encoding="utf-8", errors="ignore")
            try:
                parsed = parse_bib_file(raw_text)
                current_keys = set(parsed.keys())
            except Exception:
                current_keys = set()
            if not current_keys:
                # Regex fallback for minor syntax errors
                fallback_keys = re.findall(r"@[a-zA-Z]+\s*\{\s*([^,\s]+)\s*,", raw_text)
                current_keys = set(fallback_keys)

        # Parse target references.bib keys from target_sha
        target_keys: set[str] = set()
        show_bib_res = self._run_git(p_dir, "show", f"{valid_sha}:references.bib", check=False)
        if show_bib_res.returncode == 0 and show_bib_res.stdout.strip():
            target_raw = show_bib_res.stdout
            try:
                parsed_target = parse_bib_file(target_raw)
                target_keys = set(parsed_target.keys())
            except Exception:
                target_keys = set()
            if not target_keys:
                fallback_target = re.findall(r"@[a-zA-Z]+\s*\{\s*([^,\s]+)\s*,", target_raw)
                target_keys = set(fallback_target)

        lost_keys = sorted(k for k in current_keys if k not in target_keys)
        has_warning = len(lost_keys) > 0

        return RestorePreview(
            commit_sha=valid_sha,
            changed_files=changed_files,
            unified_diff=unified_diff,
            lost_bib_keys=lost_keys,
            has_bib_loss_warning=has_warning,
        )

    async def restore_snapshot(
        self,
        project_id: str,
        project_dir: Path,
        target_sha: str,
        project_service: ProjectService,
        compile_lock: asyncio.Lock,
        unsaved_buffers: dict[str, str] | None = None,
    ) -> SnapshotInfo:
        valid_sha = validate_commit_sha(target_sha)
        async with compile_lock:
            p_dir = Path(project_dir).resolve()
            self.ensure_repo(p_dir)

            # 1. Validate and save unsaved buffers (rejecting path traversal and protected directories)
            if unsaved_buffers:
                for rel_path, content in unsaved_buffers.items():
                    clean_path = Path(rel_path)
                    if clean_path.is_absolute() or ".." in clean_path.parts:
                        raise ValueError(f"Path traversal detected in buffer path: {rel_path}")
                    if any(part in {".git", ".build", ".texmf"} for part in clean_path.parts):
                        raise PermissionError(f"Access to protected path rejected: {rel_path}")
                    target_file = (p_dir / clean_path).resolve()
                    try:
                        target_file.relative_to(p_dir)
                    except ValueError:
                        raise PermissionError(f"Buffer path traverses outside project: {rel_path}")
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_text(content, encoding="utf-8")

            # 2. Create pre_restore snapshot
            pre_snap = await asyncio.to_thread(
                self.create_snapshot,
                project_dir=p_dir,
                trigger="pre_restore",
                message=f"Pre-restore checkpoint before restoring {valid_sha[:8]}",
            )

            # 3. Restore files to target_sha
            # Find files tracked in HEAD that do not exist in target_sha
            head_files_res = await asyncio.to_thread(self._run_git, p_dir, "ls-tree", "-r", "--name-only", "HEAD", check=False)
            head_files = set(head_files_res.stdout.splitlines()) if head_files_res.returncode == 0 else set()

            target_files_res = await asyncio.to_thread(self._run_git, p_dir, "ls-tree", "-r", "--name-only", valid_sha, check=False)
            if target_files_res.returncode != 0:
                raise ValueError(f"Target commit {valid_sha} does not exist in repository.")
            target_files = set(target_files_res.stdout.splitlines())

            files_to_remove = head_files - target_files
            for rel in files_to_remove:
                # Do not delete paper-reviews or anything in .gitignore
                if rel.startswith("paper-reviews/") or rel == ".gitignore":
                    continue
                victim = p_dir / rel
                if victim.is_file():
                    try:
                        victim.unlink()
                    except OSError:
                        pass

            # Restore tracked files from target_sha
            await asyncio.to_thread(self._run_git, p_dir, "checkout", valid_sha, "--", ".")

            # Commit the restored state so working tree and git index are clean
            restore_msg = f"[manual] Restored working copy to {valid_sha[:8]}"
            await asyncio.to_thread(self._run_git, p_dir, "add", "-A")
            await asyncio.to_thread(self._run_git, p_dir, "commit", "-m", restore_msg, check=False)

            log_res = await asyncio.to_thread(self._run_git, p_dir, "log", "-1", "--format=%H|%cI|%s")
            restored_parts = log_res.stdout.strip().split("|", 2)
            restored_sha = restored_parts[0] if restored_parts else valid_sha
            restored_ts = restored_parts[1] if len(restored_parts) > 1 else datetime.now(timezone.utc).isoformat()

            return pre_snap or SnapshotInfo(
                commit_sha=restored_sha,
                timestamp=restored_ts,
                trigger="pre_restore",
                message=restore_msg,
            )

    def compact_old_snapshots(
        self,
        project_dir: Path,
        keep_recent: int = 100,
    ) -> int:
        p_dir = Path(project_dir).resolve()
        self.ensure_repo(p_dir)

        # Find pinned SHAs in paper-reviews/*.md
        pinned_shas: set[str] = set()
        reviews_dir = p_dir / "paper-reviews"
        if reviews_dir.is_dir():
            for r_file in reviews_dir.glob("*.md"):
                try:
                    text = r_file.read_text(encoding="utf-8", errors="ignore")
                    matches = _SHA_HEX_RE.findall(text)
                    for m in matches:
                        pinned_shas.add(m.lower())
                except OSError:
                    pass

        # Also get all tagged commit SHAs (peeling annotated tags)
        tag_res = self._run_git(p_dir, "for-each-ref", "--format=%(objectname) %(*objectname)", "refs/tags", check=False)
        if tag_res.returncode == 0:
            for line in tag_res.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1]:
                    pinned_shas.add(parts[1].lower())
                elif len(parts) >= 1 and parts[0]:
                    pinned_shas.add(parts[0].lower())

        # Read full log
        log_res = self._run_git(p_dir, "log", "--format=%H|%s", check=False)
        if log_res.returncode != 0 or not log_res.stdout.strip():
            return 0

        commits: list[tuple[str, str]] = []
        for line in log_res.stdout.strip().splitlines():
            parts = line.split("|", 1)
            if parts:
                commits.append((parts[0].lower(), parts[1] if len(parts) > 1 else ""))

        if len(commits) <= keep_recent:
            return 0

        # Most recent keep_recent are pinned
        for sha, _ in commits[:keep_recent]:
            pinned_shas.add(sha)

        # Root commit is also pinned
        if commits:
            pinned_shas.add(commits[-1][0])

        # Identify pre_restore / manual commits as pinned
        for sha, msg in commits:
            m = _TRIGGER_RE.match(msg)
            if m and m.group(1) in {"pre_restore", "manual"}:
                pinned_shas.add(sha)

        # Ensure any short SHA reference in paper-reviews matches full commit sha
        for c_sha, _ in commits:
            if any(c_sha.startswith(pin) or pin.startswith(c_sha) for pin in pinned_shas):
                pinned_shas.add(c_sha)

        # Commits are in reverse chronological order: [newest, ..., oldest]
        # Any commit not in pinned_shas can be compacted via git replace -f --graft
        compacted_count = 0
        i = 0
        while i < len(commits) - 1:
            curr_sha, _ = commits[i]
            # If the next commit is unpinned, find the next pinned ancestor
            next_sha, _ = commits[i + 1]
            if next_sha not in pinned_shas:
                # Find next pinned commit down the chain
                j = i + 1
                while j < len(commits) and commits[j][0] not in pinned_shas:
                    j += 1
                if j < len(commits):
                    target_parent = commits[j][0]
                    # Graft curr_sha directly onto target_parent with -f
                    graft_res = self._run_git(p_dir, "replace", "-f", "--graft", curr_sha, target_parent, check=False)
                    if graft_res.returncode == 0:
                        compacted_count += (j - (i + 1))
                    i = j
                    continue
            i += 1

        return compacted_count
