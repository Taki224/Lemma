from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.latex.ctan_helper import (
    InvalidPackageNameError,
    MissingPackageError,
    PackageInstallResult,
    detect_missing_packages,
    install_missing_packages,
    resolve_ctan_package_name,
    save_manual_sty_upload,
    validate_package_or_filename,
)

HAS_KPSEWHICH = (
    shutil.which(
        "kpsewhich",
        path="/Library/TeX/texbin:/usr/local/texlive/bin:/usr/bin:/bin",
    )
    is not None
)


def test_detect_missing_packages_parses_sty_and_cls() -> None:
    sample_log = """
This is pdfTeX, Version 3.141592653-2.6-1.40.26 (TeX Live 2024)
(./main.tex
LaTeX2e <2023-11-01> patch level 1
! LaTeX Error: File `academic-manuscript.cls' not found.

Type X to quit or <RETURN> to proceed,
or enter new name. (Default extension: cls)

! LaTeX Error: File 'booktabs.sty' not found.
! LaTeX Error: File `tikz.sty' not found.
"""
    missing = detect_missing_packages(sample_log)
    assert missing == ["academic-manuscript.cls", "booktabs.sty", "tikz.sty"]

    err = MissingPackageError(
        package_name="booktabs.sty",
        raw_message="! LaTeX Error: File 'booktabs.sty' not found.",
    )
    assert err.package_name == "booktabs.sty"
    assert "booktabs.sty" in str(err)


def test_invalid_or_leading_dash_package_name_rejected() -> None:
    for invalid_name in [
        "-option",
        "--repository=http://evil.example",
        "../secret.sty",
        "foo/bar.sty",
        "pkg;rm -rf /",
        "pkg$(id).sty",
        "",
        ".hidden",
        "_leading_underscore",
    ]:
        with pytest.raises(InvalidPackageNameError):
            validate_package_or_filename(invalid_name)

    assert validate_package_or_filename("booktabs.sty") == "booktabs.sty"
    assert validate_package_or_filename("academic-manuscript.cls") == "academic-manuscript.cls"
    assert validate_package_or_filename("pgf") == "pgf"


async def test_resolve_ctan_package_maps_file_to_ctan_bundle_and_passes_double_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs):
        recorded_cmds.append(list(cmd))
        if "search" in cmd:
            stdout = "pgf:\n\ttexmf-dist/tex/latex/pgf/frontendlayer/tikz.sty\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if "init-usertree" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "install" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="tlmgr: package repository ...\ninstall: pgf\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("app.latex.ctan_helper.subprocess.run", fake_run)

    pkg = resolve_ctan_package_name("tikz.sty")
    assert pkg == "pgf"
    assert any("search" in c and "--global" in c and "--file" in c for c in recorded_cmds)

    texmf_dir = tmp_path / "texmf"
    texmf_dir.mkdir()

    result: PackageInstallResult = await install_missing_packages(
        names=["tikz.sty"],
        texmf_dir=texmf_dir,
    )
    assert result.installed == ["pgf"]
    assert result.failed == []
    assert result.offline is False

    install_cmds = [c for c in recorded_cmds if "install" in c]
    assert len(install_cmds) == 1
    install_cmd = install_cmds[0]
    assert "--usermode" in install_cmd
    assert "--usertree" in install_cmd
    assert str(texmf_dir) in install_cmd
    dd_index = install_cmd.index("--")
    assert install_cmd[dd_index + 1 :] == ["pgf"]


async def test_offline_failure_returns_manual_upload_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_offline_run(cmd: list[str], **kwargs):
        if "search" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="tlmgr: cannot setup TLPDB: could not download mirror.ctan.org",
            )
        if "init-usertree" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="tlmgr: Cannot load TeX Live database from http://mirror.ctan.org: Network is unreachable",
        )

    monkeypatch.setattr("app.latex.ctan_helper.subprocess.run", fake_offline_run)

    texmf_dir = tmp_path / "texmf"
    texmf_dir.mkdir()

    result = await install_missing_packages(
        names=["custom-algo.sty"],
        texmf_dir=texmf_dir,
    )
    assert result.installed == []
    assert result.failed == ["custom-algo"]
    assert result.offline is True
    assert result.manual_upload_hint is not None
    assert "custom-algo.sty" in result.manual_upload_hint


@pytest.mark.requires_tex
@pytest.mark.skipif(not HAS_KPSEWHICH, reason="TeX Live kpsewhich not installed on host")
def test_manual_sty_upload_isolates_to_project_and_blocks_system_shadowing(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "proj1"
    project_dir.mkdir()
    texmf_dir = tmp_path / "global_texmf"
    texmf_dir.mkdir()

    # Uploading a novel custom package into the project succeeds
    custom_content = b"\\ProvidesPackage{my_offline_pkg}[2026/01/01 Custom]\n"
    saved_path = save_manual_sty_upload(
        filename="my_offline_pkg.sty",
        content=custom_content,
        project_dir=project_dir,
        texmf_dir=texmf_dir,
    )
    expected_path = project_dir / ".texmf" / "tex" / "latex" / "manual" / "my_offline_pkg.sty"
    assert saved_path == expected_path
    assert saved_path.read_bytes() == custom_content

    # Attempting to shadow a core system class/package like article.cls must be rejected
    with pytest.raises(PermissionError, match="shadow"):
        save_manual_sty_upload(
            filename="article.cls",
            content=b"\\ProvidesClass{article}[malicious override]\n",
            project_dir=project_dir,
            texmf_dir=texmf_dir,
        )


def test_detect_missing_packages_parses_missing_font_files() -> None:
    sample_log = (
        "/usr/local/texlive/2026/texmf-dist/tex/latex/base/fontenc.sty:116: Font "
        "T1/phv/m/n/10.95=phvr8t at 10.95pt not loadable: Metric (TFM) file not found.\n"
        "!pdfTeX error: pdflatex (file uhvr8a): Font uhvr8a at 1095 not found\n"
    )
    assert detect_missing_packages(sample_log) == ["phvr8t.tfm", "uhvr8a.pfb"]


async def test_font_package_install_registers_its_map_with_updmap_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    texmf_dir = tmp_path / "texmf"
    (texmf_dir / "tlpkg").mkdir(parents=True)
    (texmf_dir / "tlpkg" / "texlive.tlpdb").write_text(
        "name helvetic\ncategory Package\nexecute addMap uhv.map\n\n"
        "name pgf\ncategory Package\n",
        encoding="utf-8",
    )
    recorded: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd: list[str], **kwargs):
        recorded.append((list(cmd), kwargs.get("env") or {}))
        if "search" in cmd:
            stdout = "helvetic:\n\ttexmf-dist/fonts/tfm/adobe/helvetic/phvr8t.tfm\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("app.latex.ctan_helper.subprocess.run", fake_run)

    result = await install_missing_packages(names=["phvr8t.tfm"], texmf_dir=texmf_dir)

    assert result.installed == ["helvetic"]
    assert (texmf_dir / "web2c" / "updmap.cfg").read_text(encoding="utf-8") == "Map uhv.map\n"
    updmap_calls = [env for cmd, env in recorded if cmd[0].endswith("updmap-user")]
    assert len(updmap_calls) == 1
    assert updmap_calls[0]["TEXMFVAR"] == str(texmf_dir.resolve())
    assert updmap_calls[0]["TEXMFHOME"] == str(texmf_dir.resolve())


def test_detect_missing_packages_parses_bibliography_language_and_support_files() -> None:
    sample_log = (
        "I couldn't open style file IEEEtranS.bst\n"
        "! Package biblatex Error: Style 'ieee' not found.\n"
        "! Package biblatex Error: Citation style 'apa' not found.\n"
        "! Package babel Error: Unknown option 'ngerman'. Either you misspelled it\n"
        "(babel)                or the language definition file ngerman.ldf\n"
        "(babel)                was not found.\n"
        "! LaTeX Error: File `size11.clo' not found.\n"
        "! LaTeX Error: File `tikzlibraryshapes.geometric.code.tex' not found.\n"
        "!pdfTeX error: pdflatex (file t5-ec.enc): cannot open encoding file for reading\n"
    )
    assert detect_missing_packages(sample_log) == [
        "size11.clo",
        "tikzlibraryshapes.geometric.code.tex",
        "t5-ec.enc",
        "IEEEtranS.bst",
        "ieee.bbx",
        "apa.cbx",
        "ngerman.ldf",
    ]


async def test_unresolvable_non_package_file_is_not_installed_by_stem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("app.latex.ctan_helper.subprocess.run", fake_run)

    texmf_dir = tmp_path / "texmf"
    texmf_dir.mkdir()
    result = await install_missing_packages(names=["MyStyle.bst"], texmf_dir=texmf_dir)

    assert result.installed == []
    assert result.failed == ["MyStyle.bst"]
    assert result.manual_upload_hint is not None
    assert not any("install" in c for c in recorded)
