from __future__ import annotations

import gzip
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.latex.sandbox import _build_safe_path_env

FALLBACK_HINT = "No exact source mapping — jumped to nearest page position"

_STAGE_PREFIX_RE = re.compile(r"^.*?/lemma_tex_stage_[^/]+/(?:\./)?")
_WORK_PREFIX_RE = re.compile(r"^/work/(?:\./)?")


@dataclass(frozen=True)
class ForwardSyncResult:
    page: int
    x: float
    y: float
    h: float
    v: float
    exact: bool
    hint: str | None = None
    width: float = 0.0
    height: float = 0.0


@dataclass(frozen=True)
class InverseSyncResult:
    file_path: str
    line: int
    column: int
    exact: bool
    hint: str | None = None


def _normalize_tex_rel_path(raw_path: str, build_dir: Path | None = None) -> str:
    cleaned = raw_path.strip().replace("\\", "/")
    cleaned = _STAGE_PREFIX_RE.sub("", cleaned)
    cleaned = _WORK_PREFIX_RE.sub("", cleaned)

    if build_dir is not None:
        project_dir = build_dir.resolve().parent
        for prefix in (str(build_dir.resolve()), str(project_dir)):
            norm_prefix = prefix.replace("\\", "/").rstrip("/") + "/"
            if cleaned.startswith(norm_prefix):
                cleaned = cleaned[len(norm_prefix) :]

    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    # Collapse any remaining /./ segments
    cleaned = re.sub(r"/\./", "/", cleaned)
    return cleaned.lstrip("/")


def _read_synctex_input_map(build_dir: Path, pdf_name: str) -> dict[str, str]:
    """Map normalized relative project paths to the exact Input path strings stored in .synctex.gz."""
    stem = Path(pdf_name).stem
    gz_path = build_dir / f"{stem}.synctex.gz"
    plain_path = build_dir / f"{stem}.synctex"

    lines: list[str] = []
    try:
        if gz_path.is_file():
            with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f):
                    if line.startswith("Input:"):
                        lines.append(line.strip())
                    elif line.startswith("Content:") or idx > 2000:
                        break
        elif plain_path.is_file():
            with plain_path.open("r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f):
                    if line.startswith("Input:"):
                        lines.append(line.strip())
                    elif line.startswith("Content:") or idx > 2000:
                        break
    except OSError:
        return {}

    mapping: dict[str, str] = {}
    for line in lines:
        # Format: Input:<tag>:<path>
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        recorded_path = parts[2].strip()
        norm_rel = _normalize_tex_rel_path(recorded_path, build_dir=build_dir)
        if norm_rel and not norm_rel.startswith("/"):
            mapping[norm_rel] = recorded_path
    return mapping


@dataclass(frozen=True)
class _ForwardBox:
    page: int
    x: float
    y: float
    h: float
    v: float
    width: float
    height: float


def _parse_forward_output(raw_output: str) -> _ForwardBox | None:
    page: int | None = None
    x: float | None = None
    y: float | None = None
    h: float | None = None
    v: float | None = None
    width: float | None = None
    height: float | None = None

    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if line.startswith("Page:") and page is None:
            try:
                page = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("x:") and x is None:
            try:
                x = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("y:") and y is None:
            try:
                y = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("h:") and h is None:
            try:
                h = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("v:") and v is None:
            try:
                v = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("W:") and width is None:
            try:
                width = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("H:") and height is None:
            try:
                height = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass

    if page is not None and (x is not None or h is not None) and (y is not None or v is not None):
        resolved_x = x if x is not None else (h or 0.0)
        resolved_y = y if y is not None else (v or 0.0)
        resolved_h = h if h is not None else resolved_x
        resolved_v = v if v is not None else resolved_y
        return _ForwardBox(
            page=page,
            x=resolved_x,
            y=resolved_y,
            h=resolved_h,
            v=resolved_v,
            width=width if width is not None else 0.0,
            height=height if height is not None else 0.0,
        )

    return None


def _parse_inverse_output(
    raw_output: str, build_dir: Path
) -> tuple[str, int, int] | None:
    input_file: str | None = None
    line_num: int | None = None
    col_num: int = 1

    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if line.startswith("Input:") and input_file is None:
            input_file = line.split(":", 1)[1].strip()
        elif line.startswith("Line:") and line_num is None:
            try:
                line_num = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("Column:"):
            try:
                col_val = int(line.split(":", 1)[1].strip())
                col_num = max(1, col_val)
            except ValueError:
                pass

        if input_file is not None and line_num is not None:
            norm_path = _normalize_tex_rel_path(input_file, build_dir=build_dir)
            return (norm_path, max(1, line_num), col_num)

    return None


class SyncTexService:
    """Provides forward (.tex -> PDF) and inverse (PDF -> .tex) SyncTeX coordinate resolution."""

    def _find_synctex_binary(self) -> str:
        safe_path = _build_safe_path_env()
        found = shutil.which("synctex", path=safe_path)
        return found or "synctex"

    def _run_synctex_cli(self, args: list[str], cwd: Path) -> str:
        synctex_bin = self._find_synctex_binary()
        try:
            proc = subprocess.run(
                [synctex_bin, *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            return proc.stdout or ""
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""

    def forward_sync(
        self,
        build_dir: Path,
        pdf_name: str,
        tex_file: str,
        line: int,
        column: int = 1,
    ) -> ForwardSyncResult:
        build_dir = Path(build_dir).resolve()
        norm_tex = _normalize_tex_rel_path(tex_file, build_dir=build_dir)
        input_map = _read_synctex_input_map(build_dir, pdf_name)
        recorded_target = input_map.get(norm_tex, norm_tex)

        candidate_targets = [recorded_target]
        if norm_tex not in candidate_targets:
            candidate_targets.append(norm_tex)
        if f"./{norm_tex}" not in candidate_targets:
            candidate_targets.append(f"./{norm_tex}")

        # 1. Exact line lookup
        for target_str in candidate_targets:
            out = self._run_synctex_cli(
                ["view", "-i", f"{line}:{column}:{target_str}", "-o", pdf_name],
                cwd=build_dir,
            )
            parsed = _parse_forward_output(out)
            if parsed is not None:
                return ForwardSyncResult(
                    page=parsed.page,
                    x=parsed.x,
                    y=parsed.y,
                    h=parsed.h,
                    v=parsed.v,
                    exact=True,
                    hint=None,
                    width=parsed.width,
                    height=parsed.height,
                )

        # 2. Fallback search across nearby lines when inside a float or unmapped macro
        seen_lines: set[int] = {line}
        candidate_lines: list[int] = []
        for delta in range(1, 15):
            for candidate in (line - delta, line + delta):
                if candidate >= 1 and candidate not in seen_lines:
                    seen_lines.add(candidate)
                    candidate_lines.append(candidate)

        for candidate_line in candidate_lines[:16]:
            for target_str in candidate_targets:
                out = self._run_synctex_cli(
                    ["view", "-i", f"{candidate_line}:{column}:{target_str}", "-o", pdf_name],
                    cwd=build_dir,
                )
                parsed = _parse_forward_output(out)
                if parsed is not None:
                    return ForwardSyncResult(
                        page=parsed.page,
                        x=parsed.x,
                        y=parsed.y,
                        h=parsed.h,
                        v=parsed.v,
                        exact=False,
                        hint=FALLBACK_HINT,
                        width=parsed.width,
                        height=parsed.height,
                    )

        return ForwardSyncResult(
            page=1,
            x=72.0,
            y=72.0,
            h=72.0,
            v=72.0,
            exact=False,
            hint=FALLBACK_HINT,
        )

    def inverse_sync(
        self,
        build_dir: Path,
        pdf_name: str,
        page: int,
        x: float,
        y: float,
    ) -> InverseSyncResult:
        build_dir = Path(build_dir).resolve()
        out = self._run_synctex_cli(
            ["edit", "-o", f"{page}:{x}:{y}:{pdf_name}"],
            cwd=build_dir,
        )
        parsed = _parse_inverse_output(out, build_dir=build_dir)
        if parsed is not None:
            file_path, line_num, col_num = parsed
            return InverseSyncResult(
                file_path=file_path,
                line=line_num,
                column=col_num,
                exact=True,
                hint=None,
            )

        return InverseSyncResult(
            file_path="main.tex",
            line=1,
            column=1,
            exact=False,
            hint=FALLBACK_HINT,
        )
