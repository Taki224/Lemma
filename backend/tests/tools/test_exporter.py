import os
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from app.config import AppSettings
from app.latex.compiler import CompileOrchestrator, CompileReport
from app.latex.sandbox import SandboxResult
from app.tools.exporter import (
    parse_fls_inputs,
    export_camera_ready_zip,
    ExportResult,
)


def test_parse_fls_inputs_and_strip_unused_files(tmp_path: Path):
    project_dir = tmp_path / "my_project"
    project_dir.mkdir(parents=True)
    (project_dir / "main.tex").write_text(r"\documentclass{article}\input{chapter1}\begin{document}Hello\end{document}")
    (project_dir / "chapter1.tex").write_text("Chapter 1 content")
    (project_dir / "references.bib").write_text("@article{k, title={T}}")
    (project_dir / "used_figure.png").write_bytes(b"used-img")
    (project_dir / "unused.png").write_bytes(b"unused-img")

    # Reviews dir and git dir
    reviews_dir = project_dir / "paper-reviews"
    reviews_dir.mkdir()
    (reviews_dir / "review1.md").write_text("# Review")

    build_dir = project_dir / ".build"
    build_dir.mkdir()
    (build_dir / "main.bbl").write_text("bbl content")

    # Custom package in global texmf
    texmf_dir = tmp_path / "texmf"
    pkg_dir = texmf_dir / "tex" / "latex" / "mypkg"
    pkg_dir.mkdir(parents=True)
    custom_sty = pkg_dir / "mypkg.sty"
    custom_sty.write_text(r"\ProvidesPackage{mypkg}")

    # Simulated .fls content
    fls_content = f"""PWD {project_dir}
INPUT /usr/share/texlive/texmf-dist/tex/latex/base/article.cls
INPUT {project_dir}/main.tex
INPUT {project_dir}/chapter1.tex
INPUT {project_dir}/used_figure.png
INPUT {custom_sty}
"""

    inputs = parse_fls_inputs(
        fls_content=fls_content,
        project_dir=project_dir,
        texmf_dir=texmf_dir,
    )

    included_rel_paths = [dest for src, dest in inputs.included_files]
    assert "main.tex" in included_rel_paths
    assert "chapter1.tex" in included_rel_paths
    assert "used_figure.png" in included_rel_paths
    assert "references.bib" in included_rel_paths
    assert "main.bbl" in included_rel_paths
    assert "mypkg.sty" in included_rel_paths

    # Stripped files
    assert "unused.png" in inputs.stripped_files
    assert any("paper-reviews" in s for s in inputs.stripped_files)


def test_parse_fls_rejects_external_symlinks(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("secret")

    symlink_file = project_dir / "leak.tex"
    try:
        os.symlink(outside_file, symlink_file)
    except OSError:
        pytest.skip("Symlinks not supported in environment")

    fls_content = f"""PWD {project_dir}
INPUT {symlink_file}
"""
    with pytest.raises(PermissionError, match=r"(?i)symlink"):
        parse_fls_inputs(fls_content, project_dir=project_dir)

    # Also test symlink .bib escape
    os.unlink(symlink_file)
    symlink_bib = project_dir / "leak.bib"
    try:
        os.symlink(outside_file, symlink_bib)
    except OSError:
        pass
    with pytest.raises(PermissionError, match=r"(?i)symlink"):
        parse_fls_inputs(f"PWD {project_dir}\n", project_dir=project_dir)


@pytest.mark.asyncio
async def test_export_camera_ready_zip_workflow(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    (project_dir / "main.tex").write_text(r"\documentclass{article}\begin{document}Hi\end{document}")
    (project_dir / "unused.png").write_bytes(b"unused")

    build_dir = project_dir / ".build"
    build_dir.mkdir()
    (build_dir / "main.fls").write_text(f"PWD {project_dir}\nINPUT {project_dir}/main.tex\n")
    (build_dir / "main.bbl").write_text("bbl")

    mock_compiler = MagicMock(spec=CompileOrchestrator)
    mock_compiler.compile_project = AsyncMock(
        return_value=CompileReport(
            project_id="p1",
            root_tex="main.tex",
            status="success",
            returncode=0,
            duration_ms=100,
            sandbox_mode="uid_rlimit",
            fls_path=build_dir / "main.fls",
        )
    )

    settings = AppSettings()
    out_zip = tmp_path / "export.zip"

    # Mock sandboxed verification compile
    async def mock_run_sandboxed(*args, **kwargs):
        # Create a dummy pdf in verify build dir to simulate success
        b_dir = kwargs.get("build_dir") or args[1]
        b_dir.mkdir(parents=True, exist_ok=True)
        (b_dir / "main.pdf").write_bytes(b"fake-pdf")
        return SandboxResult(
            returncode=0,
            stdout="Verification compile success",
            stderr="",
            timed_out=False,
            sandbox_mode="uid_rlimit",
            duration_ms=50,
        )

    res = await export_camera_ready_zip(
        project_id="p1",
        project_dir=project_dir,
        compiler=mock_compiler,
        settings=settings,
        output_zip_path=out_zip,
        run_tex_func=mock_run_sandboxed,
    )

    assert isinstance(res, ExportResult)
    assert res.verification_compile_passed
    assert out_zip.is_file()
    assert "unused.png" in res.stripped_files
    assert "main.tex" in res.included_files

    with zipfile.ZipFile(out_zip, "r") as zf:
        namelist = zf.namelist()
        assert "main.tex" in namelist
        assert "unused.png" not in namelist


@pytest.mark.requires_tex
@pytest.mark.asyncio
async def test_exported_zip_includes_used_assets_bib_bbl_and_custom_sty_and_compiles_with_empty_texmfhome(tmp_path: Path):
    # Integration test requiring actual TeX Live
    project_dir = tmp_path / "manuscript"
    project_dir.mkdir(parents=True)
    texmf_dir = tmp_path / "texmf"
    texmf_dir.mkdir(parents=True)

    # Custom package in texmf
    sty_dir = texmf_dir / "tex" / "latex" / "customfeat"
    sty_dir.mkdir(parents=True)
    (sty_dir / "customfeat.sty").write_text(r"""\NeedsTeXFormat{LaTeX2e}
\ProvidesPackage{customfeat}[2026/01/01 Custom Feat]
\newcommand{\customhello}{Custom Hello World}
""")

    (project_dir / "references.bib").write_text("""@article{sample,
  title={Sample},
  author={Author},
  year={2026}
}
""")
    (project_dir / "main.tex").write_text(r"""\documentclass{article}
\usepackage{customfeat}
\begin{document}
\customhello
\cite{sample}
\bibliographystyle{plain}
\bibliography{references}
\end{document}
""")
    (project_dir / "unused.png").write_bytes(b"unused")
    rev_dir = project_dir / "paper-reviews"
    rev_dir.mkdir()
    (rev_dir / "r1.md").write_text("Review")

    settings = AppSettings(data_dir=tmp_path)
    compiler = CompileOrchestrator(settings=settings)

    out_zip = tmp_path / "camera_ready.zip"
    res = await export_camera_ready_zip(
        project_id="manuscript",
        project_dir=project_dir,
        compiler=compiler,
        settings=settings,
        output_zip_path=out_zip,
        texmf_dir=texmf_dir,
    )

    assert res.verification_compile_passed
    assert "unused.png" in res.stripped_files
    assert any("r1.md" in s for s in res.stripped_files)
    assert "customfeat.sty" in res.included_files
    assert "references.bib" in res.included_files
