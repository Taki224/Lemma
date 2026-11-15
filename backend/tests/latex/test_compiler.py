from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import bootstrap_data_dir
from app.latex.compiler import CompileOrchestrator, CompileReport, detect_root_tex
from app.latex.ctan_helper import PackageInstallResult
from app.latex.sandbox import SandboxResult


def test_detect_root_tex_finds_documentclass_and_honors_override(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "manuscript_proj"
    (project_dir / "chapters").mkdir(parents=True)

    (project_dir / "chapters" / "intro.tex").write_text(
        r"\section{Introduction}" + "\nSome text.\n",
        encoding="utf-8",
    )
    (project_dir / "appendix.tex").write_text(
        "% \\documentclass{article}\n\\section{Appendix}\n",
        encoding="utf-8",
    )
    (project_dir / "other_doc.tex").write_text(
        "\\documentclass{article}\n\\begin{document}Other\\end{document}\n",
        encoding="utf-8",
    )
    (project_dir / "main.tex").write_text(
        "\\documentclass[12pt]{report}\n\\begin{document}\n\\input{chapters/intro.tex}\n\\end{document}\n",
        encoding="utf-8",
    )

    # Prefers main.tex when multiple files have \documentclass
    assert detect_root_tex(project_dir) == "main.tex"

    # Honors explicit configured_root override when present
    assert detect_root_tex(project_dir, configured_root="other_doc.tex") == "other_doc.tex"

    # Falls back to scanning if configured_root does not exist
    assert detect_root_tex(project_dir, configured_root="nonexistent.tex") == "main.tex"


async def test_auto_heal_loop_installs_missing_package_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir)
    project_dir = settings.projects_dir / "proj1"
    project_dir.mkdir(parents=True)
    (project_dir / "main.tex").write_text(
        "\\documentclass{article}\n\\usepackage{booktabs}\n\\begin{document}Hi\\end{document}\n",
        encoding="utf-8",
    )

    compile_calls = 0
    install_calls: list[list[str]] = []
    hook_calls: list[tuple[str, Path]] = []

    async def fake_run_sandboxed_tex(
        project_dir: Path,
        build_dir: Path,
        root_tex: str,
        settings,
        isolated_texmf: bool = False,
    ) -> SandboxResult:
        nonlocal compile_calls
        compile_calls += 1
        build_dir.mkdir(parents=True, exist_ok=True)
        if compile_calls == 1:
            (build_dir / "main.log").write_text(
                "! LaTeX Error: File `booktabs.sty' not found.\n",
                encoding="utf-8",
            )
            return SandboxResult(
                returncode=1,
                stdout="",
                stderr="",
                timed_out=False,
                sandbox_mode="uid_rlimit",
                duration_ms=45,
            )
        (build_dir / "main.log").write_text("Output written on main.pdf (1 page).\n", encoding="utf-8")
        (build_dir / "main.pdf").write_bytes(b"%PDF-1.5 fake pdf")
        (build_dir / "main.synctex.gz").write_bytes(b"synctex")
        (build_dir / "main.fls").write_text("PWD .\nINPUT main.tex\n", encoding="utf-8")
        return SandboxResult(
            returncode=0,
            stdout="Output written on main.pdf",
            stderr="",
            timed_out=False,
            sandbox_mode="uid_rlimit",
            duration_ms=80,
        )

    async def fake_install_missing_packages(names: list[str], texmf_dir: Path) -> PackageInstallResult:
        install_calls.append(list(names))
        return PackageInstallResult(installed=["booktabs"], failed=[], offline=False)

    async def on_success(project_id: str, proj_dir: Path, report: CompileReport) -> None:
        hook_calls.append((project_id, proj_dir))

    monkeypatch.setattr("app.latex.compiler.run_sandboxed_tex", fake_run_sandboxed_tex)
    monkeypatch.setattr("app.latex.compiler.install_missing_packages", fake_install_missing_packages)

    orchestrator = CompileOrchestrator(settings=settings, on_compile_success=on_success)
    report = await orchestrator.compile_project(project_id="proj1", project_dir=project_dir)

    assert report.status == "success"
    assert report.attempts == 2
    assert report.installed_packages == ["booktabs"]
    assert report.missing_packages == []
    assert report.pdf_path == project_dir / ".build" / "main.pdf"
    assert report.synctex_path == project_dir / ".build" / "main.synctex.gz"
    assert install_calls == [["booktabs.sty"]]
    assert len(hook_calls) == 1
    assert hook_calls[0] == ("proj1", project_dir)


async def test_per_project_compile_lock_serializes_concurrent_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir)
    project_dir = settings.projects_dir / "proj_lock"
    project_dir.mkdir(parents=True)
    (project_dir / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Lock test\\end{document}\n",
        encoding="utf-8",
    )

    active_count = 0
    max_concurrent = 0
    events: list[str] = []

    async def slow_sandboxed_tex(
        project_dir: Path,
        build_dir: Path,
        root_tex: str,
        settings,
        isolated_texmf: bool = False,
    ) -> SandboxResult:
        nonlocal active_count, max_concurrent
        active_count += 1
        max_concurrent = max(max_concurrent, active_count)
        events.append("enter")
        await asyncio.sleep(0.05)
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "main.pdf").write_bytes(b"%PDF-1.5")
        (build_dir / "main.log").write_text("OK\n", encoding="utf-8")
        events.append("exit")
        active_count -= 1
        return SandboxResult(
            returncode=0,
            stdout="OK",
            stderr="",
            timed_out=False,
            sandbox_mode="uid_rlimit",
            duration_ms=50,
        )

    monkeypatch.setattr("app.latex.compiler.run_sandboxed_tex", slow_sandboxed_tex)

    orchestrator = CompileOrchestrator(settings=settings)
    r1, r2 = await asyncio.gather(
        orchestrator.compile_project("proj_lock", project_dir),
        orchestrator.compile_project("proj_lock", project_dir),
    )

    assert r1.status == "success"
    assert r2.status == "success"
    assert max_concurrent == 1
    assert events == ["enter", "exit", "enter", "exit"]
