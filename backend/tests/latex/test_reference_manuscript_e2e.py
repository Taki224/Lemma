from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import bootstrap_data_dir
from app.latex.compiler import CompileOrchestrator
from app.latex.ctan_helper import PackageInstallResult, _find_tex_binary, _run_cmd_sync

HAS_PDFLATEX = (
    shutil.which(
        "pdflatex",
        path="/Library/TeX/texbin:/usr/local/texlive/bin:/usr/bin:/bin",
    )
    is not None
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "reference_manuscript"


@pytest.mark.requires_tex
@pytest.mark.skipif(not HAS_PDFLATEX, reason="TeX Live pdflatex not installed on host")
async def test_reference_manuscript_compiles_restarts_without_redownload_and_warm_rebuild_under_10s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (FIXTURE_DIR / "main.tex").is_file()
    assert (FIXTURE_DIR / "academic-manuscript.cls").is_file()
    assert (FIXTURE_DIR / "chapters" / "introduction.tex").is_file()
    assert (FIXTURE_DIR / "chapters" / "methodology.tex").is_file()
    assert (FIXTURE_DIR / "references.bib").is_file()

    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir)

    project_dir = settings.projects_dir / "reference_manuscript"
    shutil.copytree(FIXTURE_DIR, project_dir)
    # Remove local manuscriptstyle.sty so cold compile exercises auto-healing into /data/texmf
    (project_dir / "manuscriptstyle.sty").unlink()

    install_invocations: list[list[str]] = []

    async def fake_ctan_install(names: list[str], texmf_dir: Path) -> PackageInstallResult:
        install_invocations.append(list(names))
        installed_list: list[str] = []
        for name in names:
            stem = Path(name).stem
            pkg_dir = texmf_dir / "tex" / "latex" / stem
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / name).write_text(
                f"\\ProvidesPackage{{{stem}}}[2026/01/01 Auto-cached CTAN package]\n"
                "\\newcommand{\\manuscriptbadge}{\\textbf{Verified Manuscript}}\n",
                encoding="utf-8",
            )
            installed_list.append(stem)
        mktexlsr_bin = _find_tex_binary("mktexlsr")
        _run_cmd_sync([mktexlsr_bin, str(texmf_dir)], timeout=15.0)
        return PackageInstallResult(installed=installed_list, failed=[], offline=False)

    monkeypatch.setattr("app.latex.compiler.install_missing_packages", fake_ctan_install)

    # 1. Cold compile from a clean /data/texmf caches any missing CTAN package into /data/texmf
    orchestrator_1 = CompileOrchestrator(settings=settings)
    cold_report = await orchestrator_1.compile_project(
        project_id="reference_manuscript",
        project_dir=project_dir,
    )

    assert cold_report.status == "success", f"Cold compile failed: {cold_report.log_excerpt}"
    assert cold_report.pdf_path is not None and cold_report.pdf_path.is_file()
    assert cold_report.pdf_path.stat().st_size > 1000
    assert cold_report.synctex_path is not None and cold_report.synctex_path.is_file()
    assert cold_report.fls_path is not None and cold_report.fls_path.is_file()
    assert len(install_invocations) == 1

    # 2. Simulate container restart: new settings & fresh CompileOrchestrator over same /data volume
    # Wipe project .build to prove /data/texmf persists the cached package without re-downloading
    shutil.rmtree(project_dir / ".build")
    restarted_settings = bootstrap_data_dir(data_dir)
    orchestrator_2 = CompileOrchestrator(settings=restarted_settings)

    restart_report = await orchestrator_2.compile_project(
        project_id="reference_manuscript",
        project_dir=project_dir,
    )
    assert restart_report.status == "success"
    assert len(install_invocations) == 1, "Must not re-download CTAN packages after container restart"

    # 3. Warm incremental compile after editing a chapter line must complete in <= 10.0s
    intro_path = project_dir / "chapters" / "introduction.tex"
    intro_text = intro_path.read_text(encoding="utf-8")
    intro_path.write_text(
        intro_text + "\n% Incremental edit verification paragraph\nAdditional empirical observation.\n",
        encoding="utf-8",
    )

    warm_report = await orchestrator_2.compile_project(
        project_id="reference_manuscript",
        project_dir=project_dir,
    )
    assert warm_report.status == "success"
    assert warm_report.duration_ms <= 10_000, f"Warm compile took {warm_report.duration_ms}ms (> 10s SLA)"
    assert len(install_invocations) == 1

    # 4. If Docker daemon and built image are available locally, verify image budget <= 1.5 GB
    docker_bin = shutil.which("docker")
    if docker_bin:
        try:
            inspect_res = subprocess.run(
                [docker_bin, "image", "inspect", "lemma-workspace:latest", "--format={{.Size}}"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if inspect_res.returncode == 0 and inspect_res.stdout.strip().isdigit():
                size_bytes = int(inspect_res.stdout.strip())
                assert size_bytes <= int(1.5 * 1024 * 1024 * 1024)
        except (OSError, subprocess.SubprocessError):
            pass
