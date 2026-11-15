from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.latex.sandbox import _build_safe_path_env

PACKAGE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
MISSING_FILE_RE = re.compile(
    r"! LaTeX Error: File [`']([a-zA-Z0-9._-]+\."
    r"(?:sty|cls|clo|def|cfg|fd|ldf|bbx|cbx|lbx|dbx|code\.tex))' not found\."
)
MISSING_TFM_RE = re.compile(
    r"=([a-zA-Z0-9_-]+)(?: at [0-9.]+pt)? not loadable: Metric \(TFM\) file not found"
)
MISSING_TYPE1_RE = re.compile(
    r"pdfTeX error: .*?\(file ([a-zA-Z0-9_-]+)\): Font \S+ at \d+ not found"
)
MISSING_FONT_SUPPORT_RE = re.compile(
    r"\(file (?:[^)\s]*/)?([a-zA-Z0-9_.-]+\.(?:pfb|enc|map))\): cannot open"
)
MISSING_BST_RE = re.compile(r"I couldn't open style file ([a-zA-Z0-9_.-]+\.bst)")
MISSING_BIBLATEX_STYLE_RE = re.compile(
    r"Package biblatex Error: (Style|Bibliography style|Citation style) '([a-zA-Z0-9_.-]+)' not found"
)
MISSING_BABEL_LDF_RE = re.compile(
    r"language definition file\s*(?:\(babel\)\s*)?([a-zA-Z0-9_-]+\.ldf)"
)
_BIBLATEX_STYLE_EXT = {"Style": ".bbx", "Bibliography style": ".bbx", "Citation style": ".cbx"}
_STEM_FALLBACK_EXTENSIONS = (".sty", ".cls")
_TLPDB_MAP_RE = re.compile(r"^execute add(Map|MixedMap) ([a-zA-Z0-9._-]+\.map)\s*$", re.MULTILINE)


class InvalidPackageNameError(ValueError):
    """Raised when a package or file name fails strict security validation."""


class MissingPackageError(Exception):
    """Represents a missing .sty or .cls package detected in a LaTeX log."""

    def __init__(self, package_name: str, raw_message: str) -> None:
        self.package_name = package_name
        self.raw_message = raw_message
        super().__init__(f"Missing LaTeX package '{package_name}': {raw_message}")


@dataclass(slots=True)
class PackageInstallResult:
    installed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    offline: bool = False
    manual_upload_hint: str | None = None


_SENSITIVE_ENV_KEYS = {
    "OPENROUTER_API_KEY",
    "WORKSPACE_PASSWORD",
    "SESSION_SECRET",
    "ZOTERO_API_KEY",
}


def _is_unsafe_path_component(name: str) -> bool:
    return not name or ".." in name or "/" in name or "\\" in name


def validate_package_or_filename(name: str) -> str:
    """Validate a CTAN package or .sty/.cls filename against strict regex."""
    if _is_unsafe_path_component(name) or not PACKAGE_NAME_RE.match(name):
        raise InvalidPackageNameError(f"Invalid or unsafe package/file name: {name!r}")
    return name


def detect_missing_packages(latex_log: str) -> list[str]:
    """Extract ordered unique missing TeX Live files (packages, styles, fonts) from LaTeX/BibTeX/Biber output."""
    candidates = [m.group(1) for m in MISSING_FILE_RE.finditer(latex_log)]
    candidates += [f"{m.group(1)}.tfm" for m in MISSING_TFM_RE.finditer(latex_log)]
    candidates += [f"{m.group(1)}.pfb" for m in MISSING_TYPE1_RE.finditer(latex_log)]
    candidates += [m.group(1) for m in MISSING_FONT_SUPPORT_RE.finditer(latex_log)]
    candidates += [m.group(1) for m in MISSING_BST_RE.finditer(latex_log)]
    candidates += [
        f"{m.group(2)}{_BIBLATEX_STYLE_EXT[m.group(1)]}"
        for m in MISSING_BIBLATEX_STYLE_RE.finditer(latex_log)
    ]
    candidates += [m.group(1) for m in MISSING_BABEL_LDF_RE.finditer(latex_log)]
    found: list[str] = []
    for candidate in candidates:
        try:
            validated = validate_package_or_filename(candidate)
        except InvalidPackageNameError:
            continue
        if validated not in found:
            found.append(validated)
    return found


def _run_cmd_sync(
    cmd: list[str],
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    run_env = {k: v for k, v in os.environ.items() if k not in _SENSITIVE_ENV_KEYS}
    run_env["PATH"] = _build_safe_path_env() + ":" + run_env.get("PATH", "")
    if env:
        run_env.update(env)
    return subprocess.run(
        cmd,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _find_tex_binary(name: str) -> str:
    safe_path = _build_safe_path_env() + ":" + os.environ.get("PATH", "")
    return shutil.which(name, path=safe_path) or name


def resolve_ctan_package_name(missing_file: str) -> str | None:
    """Map a missing TeX Live filename to its upstream CTAN package name via tlmgr search.

    Only .sty/.cls stems double as package names; for other files a failed search returns None
    rather than guessing, so an unrelated package sharing the stem is never installed.
    """
    validated = validate_package_or_filename(missing_file)
    if validated.endswith(_STEM_FALLBACK_EXTENSIONS):
        stem: str | None = validate_package_or_filename(Path(validated).stem)
    elif "." not in validated:
        stem = validated
    else:
        stem = None

    tlmgr_bin = _find_tex_binary("tlmgr")
    cmd = [tlmgr_bin, "search", "--global", "--file", f"/{validated}$"]
    try:
        res = _run_cmd_sync(cmd, timeout=30.0)
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                stripped = line.strip()
                if stripped.endswith(":") and not line.startswith((" ", "\t")):
                    pkg_candidate = stripped[:-1].strip()
                    # Strip architecture suffixes if any
                    pkg_candidate = pkg_candidate.split(".")[0]
                    try:
                        return validate_package_or_filename(pkg_candidate)
                    except InvalidPackageNameError:
                        continue
    except (OSError, subprocess.SubprocessError):
        pass

    return stem


def _is_offline_error(output: str) -> bool:
    lowered = output.lower()
    offline_markers = (
        "network is unreachable",
        "could not download",
        "cannot load tex live database",
        "cannot setup tlpdb",
        "connection refused",
        "name or service not known",
        "temporary failure in name resolution",
        "timed out",
    )
    return any(marker in lowered for marker in offline_markers)


def _register_user_font_maps(texmf_dir: Path) -> None:
    """Enable font maps of usermode-installed packages; tlmgr --usermode skips their addMap actions.

    TEXMFVAR points at the usertree so updmap-user writes fonts/map/pdftex/updmap/pdftex.map there,
    where the compile sandbox finds it through TEXMFHOME ahead of the system map.
    """
    tlpdb = texmf_dir / "tlpkg" / "texlive.tlpdb"
    if not tlpdb.is_file():
        return
    entries = sorted(set(_TLPDB_MAP_RE.findall(tlpdb.read_text(encoding="utf-8", errors="ignore"))))
    if not entries:
        return
    web2c_dir = texmf_dir / "web2c"
    web2c_dir.mkdir(parents=True, exist_ok=True)
    (web2c_dir / "updmap.cfg").write_text(
        "".join(f"{kind} {name}\n" for kind, name in entries), encoding="utf-8"
    )
    tree = str(texmf_dir)
    try:
        _run_cmd_sync(
            [_find_tex_binary("updmap-user")],
            env={"TEXMFHOME": tree, "TEXMFVAR": tree, "TEXMFCONFIG": tree},
            timeout=120.0,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _install_missing_packages_sync(
    names: list[str],
    texmf_dir: Path,
) -> PackageInstallResult:
    texmf_dir = Path(texmf_dir).resolve()
    texmf_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(texmf_dir, 0o755)

    tlmgr_bin = _find_tex_binary("tlmgr")
    tlpkg_dir = texmf_dir / "tlpkg"
    if not tlpkg_dir.is_dir():
        try:
            _run_cmd_sync(
                [tlmgr_bin, "--usermode", "--usertree", str(texmf_dir), "init-usertree"],
                timeout=30.0,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    installed: list[str] = []
    failed: list[str] = []
    offline = False
    original_missing: list[str] = []

    for raw_name in names:
        validated = validate_package_or_filename(raw_name)
        original_missing.append(validated)
        pkg_name = resolve_ctan_package_name(validated)
        if pkg_name is None:
            if validated not in failed:
                failed.append(validated)
            continue
        validate_package_or_filename(pkg_name)

        if offline:
            if pkg_name not in failed:
                failed.append(pkg_name)
            continue

        cmd = [
            tlmgr_bin,
            "--usermode",
            "--usertree",
            str(texmf_dir),
            "install",
            "--",
            pkg_name,
        ]
        try:
            res = _run_cmd_sync(cmd, timeout=120.0)
            combined_out = f"{res.stdout}\n{res.stderr}"
            if res.returncode == 0:
                if pkg_name not in installed:
                    installed.append(pkg_name)
            else:
                if pkg_name not in failed:
                    failed.append(pkg_name)
                if _is_offline_error(combined_out):
                    offline = True
        except (OSError, subprocess.SubprocessError) as exc:
            if pkg_name not in failed:
                failed.append(pkg_name)
            if _is_offline_error(str(exc)):
                offline = True

    if installed:
        mktexlsr_bin = _find_tex_binary("mktexlsr")
        try:
            _run_cmd_sync([mktexlsr_bin, str(texmf_dir)], timeout=30.0)
        except (OSError, subprocess.SubprocessError):
            pass
        _register_user_font_maps(texmf_dir)

    manual_hint: str | None = None
    if failed:
        missing_str = ", ".join(original_missing)
        if offline:
            manual_hint = (
                f"CTAN mirror is unreachable while offline. Upload {missing_str} manually "
                "via the project .texmf uploader."
            )
        else:
            manual_hint = (
                f"Could not find {missing_str} on CTAN. If this is a custom template or style, "
                "upload the .sty/.cls file to the project."
            )

    return PackageInstallResult(
        installed=installed,
        failed=failed,
        offline=offline,
        manual_upload_hint=manual_hint,
    )


async def install_missing_packages(
    names: list[str],
    texmf_dir: Path,
) -> PackageInstallResult:
    """Install missing CTAN packages into /data/texmf using tlmgr usermode outside the compile sandbox."""
    return await asyncio.to_thread(_install_missing_packages_sync, names, texmf_dir)


def save_manual_sty_upload(
    filename: str,
    content: bytes,
    project_dir: Path,
    texmf_dir: Path,
) -> Path:
    """Save a manual .sty or .cls upload into the per-project .texmf tree after checking against system shadowing."""
    validated_name = validate_package_or_filename(filename)
    if not validated_name.endswith((".sty", ".cls", ".bst")):
        raise InvalidPackageNameError(
            f"Manual texmf upload must be a .sty, .cls, or .bst file (got {validated_name!r})"
        )

    project_dir = Path(project_dir).resolve()
    texmf_dir = Path(texmf_dir).resolve()
    project_texmf = project_dir / ".texmf"

    kpsewhich_bin = _find_tex_binary("kpsewhich")
    try:
        res = _run_cmd_sync(
            [kpsewhich_bin, validated_name],
            env={"TEXMFHOME": ""},
            timeout=10.0,
        )
        stdout_lines = res.stdout.strip().splitlines()
        found_path_str = stdout_lines[0].strip() if stdout_lines else ""
        if res.returncode == 0 and found_path_str:
            found_path = Path(found_path_str).resolve()
            if not found_path.is_relative_to(project_dir) and not found_path.is_relative_to(texmf_dir):
                raise PermissionError(
                    f"Refusing to upload '{validated_name}': cannot shadow core system TeX Live file '{found_path}'."
                )
    except PermissionError:
        raise
    except (OSError, subprocess.SubprocessError):
        pass

    if validated_name.endswith(".bst"):
        target_dir = project_texmf / "bibtex" / "bst" / "manual"
    else:
        target_dir = project_texmf / "tex" / "latex" / "manual"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / validated_name
    target_path.write_bytes(content)

    mktexlsr_bin = _find_tex_binary("mktexlsr")
    try:
        _run_cmd_sync([mktexlsr_bin, str(project_texmf)], timeout=15.0)
    except (OSError, subprocess.SubprocessError):
        pass

    return target_path
