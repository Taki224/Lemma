from __future__ import annotations

import asyncio
import inspect
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.config import AppSettings
from app.latex.ctan_helper import detect_missing_packages, install_missing_packages
from app.latex.sandbox import SandboxMode, run_sandboxed_tex

CompileStatus = Literal["success", "failed", "timeout"]
CompileSuccessCallback = (
    Callable[[str, Path, "CompileReport"], Awaitable[None] | None] | None
)

_DOCUMENTCLASS_RE = re.compile(r"\\documentclass(?:\[[^\]]*\])?\{[^}]+\}")
_EXCLUDED_SCAN_DIRS = {".git", ".build", ".texmf", "paper-reviews", "__pycache__"}
_PREFERRED_ROOT_NAMES = ("main.tex", "thesis.tex", "manuscript.tex", "paper.tex")


@dataclass(slots=True)
class CompileReport:
    project_id: str
    root_tex: str
    status: CompileStatus
    returncode: int
    duration_ms: int
    sandbox_mode: SandboxMode
    pdf_path: Path | None = None
    synctex_path: Path | None = None
    log_path: Path | None = None
    fls_path: Path | None = None
    installed_packages: list[str] = field(default_factory=list)
    missing_packages: list[str] = field(default_factory=list)
    offline_hint: str | None = None
    attempts: int = 1
    log_excerpt: str = ""


def _strip_latex_comments(text: str) -> str:
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        idx = 0
        while idx < len(line):
            pos = line.find("%", idx)
            if pos == -1:
                cleaned_lines.append(line)
                break
            backslashes = 0
            check = pos - 1
            while check >= 0 and line[check] == "\\":
                backslashes += 1
                check -= 1
            if backslashes % 2 == 0:
                cleaned_lines.append(line[:pos])
                break
            idx = pos + 1
        else:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


_GLOBAL_CTAN_LOCK = asyncio.Lock()


def _is_valid_root_file(candidate: Path, project_dir: Path) -> bool:
    return (
        candidate.is_relative_to(project_dir)
        and candidate.is_file()
        and not candidate.is_symlink()
        and candidate.name.lower().endswith(".tex")
    )


def detect_root_tex(project_dir: Path, configured_root: str | None = None) -> str:
    """Detect the root .tex file containing \\documentclass, honoring configured_root if valid."""
    project_dir = Path(project_dir).resolve()

    if configured_root:
        candidate = (project_dir / configured_root).resolve()
        if _is_valid_root_file(candidate, project_dir):
            return candidate.relative_to(project_dir).as_posix()

    all_tex_files: list[str] = []
    documentclass_files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(project_dir, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_SCAN_DIRS)
        current_dir = Path(dirpath)
        for fname in sorted(filenames):
            if not fname.endswith(".tex"):
                continue
            fpath = current_dir / fname
            if fpath.is_symlink():
                continue
            rel = fpath.resolve().relative_to(project_dir).as_posix()
            all_tex_files.append(rel)
            try:
                raw_text = fpath.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            uncommented = _strip_latex_comments(raw_text)
            if _DOCUMENTCLASS_RE.search(uncommented):
                documentclass_files.append(rel)

    if documentclass_files:
        for preferred in _PREFERRED_ROOT_NAMES:
            if preferred in documentclass_files:
                return preferred
        documentclass_files.sort(key=lambda p: (p.count("/"), p))
        return documentclass_files[0]

    if "main.tex" in all_tex_files:
        return "main.tex"
    if all_tex_files:
        return all_tex_files[0]

    raise FileNotFoundError(f"No .tex files found in project directory '{project_dir}'")


class CompileOrchestrator:
    """Coordinates per-project LaTeX builds with asyncio locks and automatic CTAN package healing."""

    def __init__(
        self,
        settings: AppSettings,
        on_compile_success: CompileSuccessCallback = None,
    ) -> None:
        self.settings = settings
        self.on_compile_success = on_compile_success
        self._locks: dict[str, asyncio.Lock] = {}

    def get_lock(self, project_id: str) -> asyncio.Lock:
        lock = self._locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[project_id] = lock
        return lock

    async def compile_project(
        self,
        project_id: str,
        project_dir: Path,
        root_tex: str | None = None,
    ) -> CompileReport:
        project_dir = Path(project_dir).resolve()
        build_dir = project_dir / ".build"
        build_dir.mkdir(parents=True, exist_ok=True)

        lock = self.get_lock(project_id)
        async with lock:
            resolved_root = detect_root_tex(project_dir, configured_root=root_tex)
            stem = Path(resolved_root).stem

            installed_packages: list[str] = []
            missing_packages: list[str] = []
            offline_hint: str | None = None
            attempted_missing: set[str] = set()
            total_duration_ms = 0
            attempts = 0

            while True:
                attempts += 1
                sandbox_res = await run_sandboxed_tex(
                    project_dir=project_dir,
                    build_dir=build_dir,
                    root_tex=resolved_root,
                    settings=self.settings,
                    isolated_texmf=False,
                )
                total_duration_ms += sandbox_res.duration_ms

                log_file = build_dir / f"{stem}.log"
                if log_file.is_file():
                    log_text = await asyncio.to_thread(log_file.read_text, "utf-8", "replace")
                else:
                    log_text = f"{sandbox_res.stdout}\n{sandbox_res.stderr}"

                if sandbox_res.timed_out:
                    return CompileReport(
                        project_id=project_id,
                        root_tex=resolved_root,
                        status="timeout",
                        returncode=sandbox_res.returncode,
                        duration_ms=total_duration_ms,
                        sandbox_mode=sandbox_res.sandbox_mode,
                        log_path=log_file if log_file.is_file() else None,
                        installed_packages=installed_packages,
                        missing_packages=missing_packages,
                        offline_hint=offline_hint,
                        attempts=attempts,
                        log_excerpt=log_text[-4000:],
                    )

                pdf_file = build_dir / f"{stem}.pdf"
                if not pdf_file.is_file():
                    alt_pdf = build_dir / Path(resolved_root).with_suffix(".pdf")
                    if alt_pdf.is_file():
                        pdf_file = alt_pdf

                if sandbox_res.returncode == 0 and pdf_file.is_file():
                    synctex_file = build_dir / f"{stem}.synctex.gz"
                    if not synctex_file.is_file():
                        alt_synctex = build_dir / f"{stem}.synctex"
                        synctex_file = alt_synctex if alt_synctex.is_file() else synctex_file
                    fls_file = build_dir / f"{stem}.fls"

                    report = CompileReport(
                        project_id=project_id,
                        root_tex=resolved_root,
                        status="success",
                        returncode=0,
                        duration_ms=total_duration_ms,
                        sandbox_mode=sandbox_res.sandbox_mode,
                        pdf_path=pdf_file,
                        synctex_path=synctex_file if synctex_file.is_file() else None,
                        log_path=log_file if log_file.is_file() else None,
                        fls_path=fls_file if fls_file.is_file() else None,
                        installed_packages=installed_packages,
                        missing_packages=[],
                        offline_hint=None,
                        attempts=attempts,
                        log_excerpt=log_text[-4000:],
                    )
                    if self.on_compile_success is not None:
                        maybe_coro = self.on_compile_success(project_id, project_dir, report)
                        if inspect.isawaitable(maybe_coro):
                            await maybe_coro
                    return report

                blg_file = build_dir / f"{stem}.blg"
                blg_text = (
                    await asyncio.to_thread(blg_file.read_text, "utf-8", "replace")
                    if blg_file.is_file()
                    else ""
                )
                detected = detect_missing_packages(
                    "\n".join([log_text, blg_text, sandbox_res.stdout, sandbox_res.stderr])
                )
                new_missing = [m for m in detected if m not in attempted_missing]
                if not new_missing:
                    missing_packages = detected
                    return CompileReport(
                        project_id=project_id,
                        root_tex=resolved_root,
                        status="failed",
                        returncode=sandbox_res.returncode,
                        duration_ms=total_duration_ms,
                        sandbox_mode=sandbox_res.sandbox_mode,
                        pdf_path=pdf_file if pdf_file.is_file() else None,
                        log_path=log_file if log_file.is_file() else None,
                        installed_packages=installed_packages,
                        missing_packages=missing_packages,
                        offline_hint=offline_hint,
                        attempts=attempts,
                        log_excerpt=log_text[-4000:],
                    )

                attempted_missing.update(new_missing)
                async with _GLOBAL_CTAN_LOCK:
                    install_res = await install_missing_packages(
                        names=new_missing,
                        texmf_dir=self.settings.texmf_dir,
                    )
                for pkg in install_res.installed:
                    if pkg not in installed_packages:
                        installed_packages.append(pkg)
                if install_res.manual_upload_hint:
                    offline_hint = install_res.manual_upload_hint

                if not install_res.installed:
                    missing_packages = detected
                    return CompileReport(
                        project_id=project_id,
                        root_tex=resolved_root,
                        status="failed",
                        returncode=sandbox_res.returncode,
                        duration_ms=total_duration_ms,
                        sandbox_mode=sandbox_res.sandbox_mode,
                        pdf_path=pdf_file if pdf_file.is_file() else None,
                        log_path=log_file if log_file.is_file() else None,
                        installed_packages=installed_packages,
                        missing_packages=missing_packages,
                        offline_hint=offline_hint,
                        attempts=attempts,
                        log_excerpt=log_text[-4000:],
                    )
