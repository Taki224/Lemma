from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets as pysecrets
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.config import AppSettings
from app.latex.compiler import CompileOrchestrator, detect_root_tex
from app.latex.sandbox import run_sandboxed_tex

logger = logging.getLogger(__name__)


@dataclass
class ExportInputs:
    included_files: list[tuple[Path, str]] = field(default_factory=list)  # (source_path, archive_rel_path)
    stripped_files: list[str] = field(default_factory=list)


@dataclass
class ExportResult:
    zip_path: Path
    included_files: list[str]
    stripped_files: list[str]
    verification_compile_passed: bool
    verification_log: str


_EXCLUDED_ARCHIVE_DIRS = {"paper-reviews", ".git", ".build", ".texmf", "__pycache__"}
_SECRET_PATTERNS = re.compile(r"(?:secrets\.ya?ml|\.env(?:$|\.)|.*_key\b|.*id_rsa\b)", re.IGNORECASE)


def parse_fls_inputs(
    fls_content: str,
    project_dir: Path,
    staging_dir: Path | None = None,
    texmf_dir: Path | None = None,
) -> ExportInputs:
    """Parses INPUT lines from .fls log, gathers used assets, custom .sty/.cls, and records stripped files."""
    project_dir = Path(project_dir).resolve()
    included_map: dict[str, Path] = {}  # archive_rel_path -> source Path
    global_texmf = Path(texmf_dir).resolve() if texmf_dir else Path("/data/texmf").resolve()
    project_texmf = (project_dir / ".texmf").resolve()

    # Parse .fls content
    for line in fls_content.splitlines():
        line = line.strip()
        if not line.startswith("INPUT "):
            continue
        raw_path_str = line[6:].strip()
        if not raw_path_str:
            continue

        raw_path = Path(raw_path_str)

        # Map staging_dir back to project_dir if applicable
        if staging_dir is not None:
            staging_res = Path(staging_dir).resolve()
            try:
                if raw_path.resolve().is_relative_to(staging_res):
                    rel = raw_path.resolve().relative_to(staging_res)
                    raw_path = project_dir / rel
            except (ValueError, OSError):
                pass

        # Normalize relative path
        if not raw_path.is_absolute():
            candidate = project_dir / raw_path
        else:
            candidate = raw_path

        # Symlink check
        if candidate.is_symlink():
            target = candidate.resolve()
            escapes_project = candidate.is_relative_to(project_dir) and not (
                target.is_relative_to(project_dir) or target.is_relative_to(global_texmf)
            )
            if escapes_project:
                raise PermissionError(f"Symlink escapes project root in exporter: {candidate} -> {target}")

        try:
            cand_res = candidate.resolve()
        except OSError:
            continue

        if not cand_res.is_file():
            continue

        # Check if file belongs to project_dir
        if cand_res.is_relative_to(project_dir):
            rel_posix = cand_res.relative_to(project_dir).as_posix()
            # Exclude build dir or internal dirs
            if rel_posix.startswith(".build/") or rel_posix.startswith(".git/"):
                continue
            included_map[rel_posix] = cand_res
            continue

        # Check if file is in custom global texmf or project texmf
        is_in_global = cand_res.is_relative_to(global_texmf) if global_texmf.is_dir() else False
        is_in_project_texmf = cand_res.is_relative_to(project_texmf) if project_texmf.is_dir() else False

        if is_in_global or is_in_project_texmf:
            # Bundle custom package at root of zip
            included_map[cand_res.name] = cand_res

    # Also include .bib, .bst files in project_dir, and generated .bbl in .build
    for bib_file in project_dir.glob("*.bib"):
        cand = bib_file.resolve()
        if not cand.is_relative_to(project_dir):
            raise PermissionError(f"Symlink escapes project root in exporter: {bib_file} -> {cand}")
        rel_posix = cand.relative_to(project_dir).as_posix()
        included_map[rel_posix] = cand

    for bst_file in project_dir.glob("*.bst"):
        cand = bst_file.resolve()
        if not cand.is_relative_to(project_dir):
            raise PermissionError(f"Symlink escapes project root in exporter: {bst_file} -> {cand}")
        rel_posix = cand.relative_to(project_dir).as_posix()
        included_map[rel_posix] = cand

    build_dir = project_dir / ".build"
    if build_dir.is_dir():
        for bbl_file in build_dir.glob("*.bbl"):
            included_map[bbl_file.name] = bbl_file.resolve()

    # Also ensure root .tex is included
    root_tex_name = detect_root_tex(project_dir)
    root_path = project_dir / root_tex_name
    if root_path.is_file():
        included_map[root_tex_name] = root_path.resolve()

    # Determine stripped files by walking project_dir
    stripped_files: list[str] = []

    # Exclude any secret files from included_map
    for arc_name, src_path in list(included_map.items()):
        if _SECRET_PATTERNS.match(src_path.name) or _SECRET_PATTERNS.match(arc_name):
            included_map.pop(arc_name, None)
            stripped_files.append(arc_name)

    included_src_set = set(included_map.values())

    for root, dirs, files in os.walk(project_dir, followlinks=False):
        curr = Path(root)
        try:
            rel_dir = curr.resolve().relative_to(project_dir).as_posix()
        except ValueError:
            continue

        # Check if directory itself is excluded
        top_part = rel_dir.split("/")[0] if rel_dir != "." else ""
        if top_part in _EXCLUDED_ARCHIVE_DIRS:
            for f in files:
                f_path = curr / f
                try:
                    stripped_files.append(f_path.resolve().relative_to(project_dir).as_posix())
                except ValueError:
                    stripped_files.append(f_path.name)
            continue

        for f in files:
            f_path = curr / f
            try:
                f_res = f_path.resolve()
            except OSError:
                continue

            try:
                rel_posix = f_res.relative_to(project_dir).as_posix()
            except ValueError:
                rel_posix = f_path.name

            # Strip secret files
            if _SECRET_PATTERNS.match(f_path.name):
                stripped_files.append(rel_posix)
                continue

            if f_res not in included_src_set:
                stripped_files.append(rel_posix)

    included_list = [(src, dest) for dest, src in included_map.items()]
    return ExportInputs(included_files=included_list, stripped_files=stripped_files)


async def export_camera_ready_zip(
    project_id: str,
    project_dir: Path,
    compiler: CompileOrchestrator,
    settings: AppSettings,
    root_tex: str | None = None,
    output_zip_path: Path | None = None,
    texmf_dir: Path | None = None,
    run_tex_func: Callable[..., Any] | None = None,
) -> ExportResult:
    """Compiles project, traces .fls, bundles used assets + custom .sty/.cls, and verifies standalone compile."""
    project_dir = Path(project_dir).resolve()
    resolved_root = detect_root_tex(project_dir, configured_root=root_tex)
    stem = Path(resolved_root).stem

    # 1. Trigger fresh compile to update .fls and .bbl
    compile_report = await compiler.compile_project(project_id, project_dir, root_tex=resolved_root)

    # 2. Locate .fls log
    build_dir = project_dir / ".build"
    fls_path = compile_report.fls_path or (build_dir / f"{stem}.fls")
    fls_content = ""
    if fls_path and fls_path.is_file():
        try:
            fls_content = fls_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    # 3. Parse inputs and stripped files
    inputs = parse_fls_inputs(
        fls_content=fls_content,
        project_dir=project_dir,
        texmf_dir=texmf_dir or getattr(settings, "texmf_dir", None),
    )

    # 4. Create ZIP archive
    if output_zip_path is None:
        build_dir.mkdir(parents=True, exist_ok=True)
        unique_suffix = pysecrets.token_hex(6)
        output_zip_path = build_dir / f"{project_id}_{unique_suffix}_camera_ready.zip"

    output_zip_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src_path, archive_name in inputs.included_files:
            # Prevent zip-slip
            norm_name = os.path.normpath(archive_name).lstrip("/\\")
            if norm_name.startswith("..") or norm_name == "..":
                continue
            zf.write(src_path, arcname=norm_name)

    # 5. Extract zip into temporary directory for standalone verification compile
    verify_passed = False
    verify_log = ""
    run_func = run_tex_func or run_sandboxed_tex

    with tempfile.TemporaryDirectory() as temp_dir:
        extract_dir = Path(temp_dir) / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(output_zip_path, "r") as zf:
            for member in zf.infolist():
                target_path = (extract_dir / member.filename).resolve()
                if not target_path.is_relative_to(extract_dir.resolve()):
                    raise PermissionError(f"Zip-slip detected in export archive: {member.filename}")
            zf.extractall(extract_dir)

        verify_build_dir = extract_dir / ".build"
        verify_build_dir.mkdir(parents=True, exist_ok=True)

        try:
            sandbox_res = await run_func(
                project_dir=extract_dir,
                build_dir=verify_build_dir,
                root_tex=resolved_root,
                settings=settings,
                isolated_texmf=True,
            )

            # Check if verification produced a PDF and returncode == 0
            verify_pdf = verify_build_dir / f"{stem}.pdf"
            if not verify_pdf.is_file():
                verify_pdf = verify_build_dir / Path(resolved_root).with_suffix(".pdf")

            if sandbox_res.returncode == 0 and verify_pdf.is_file():
                verify_passed = True
                verify_log = sandbox_res.stdout
            else:
                verify_passed = False
                verify_log = f"{sandbox_res.stdout}\n{sandbox_res.stderr}"
        except Exception as e:
            verify_passed = False
            verify_log = f"Standalone verification compile error: {e}"

    included_names = [dest for src, dest in inputs.included_files]

    return ExportResult(
        zip_path=output_zip_path,
        included_files=included_names,
        stripped_files=inputs.stripped_files,
        verification_compile_passed=verify_passed,
        verification_log=verify_log,
    )
