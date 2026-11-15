from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.config import bootstrap_data_dir
from app.latex.sandbox import (
    SandboxResult,
    build_sanitized_tex_env,
    generate_hardened_texmf_cnf,
    run_sandboxed_tex,
    stage_compile_workspace,
)

HAS_PDFLATEX = shutil.which("pdflatex", path="/Library/TeX/texbin:/usr/local/texlive/bin:/usr/bin:/bin") is not None


def test_hardened_texmf_cnf_and_trailing_colon_env_preserves_system_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_dir = tmp_path / "build"
    proj_texmf = tmp_path / "project_texmf"
    glob_texmf = tmp_path / "global_texmf"

    cnf_path = generate_hardened_texmf_cnf(
        build_dir=build_dir,
        project_texmf_dir=proj_texmf,
        global_texmf_dir=glob_texmf,
        isolated_texmf=False,
    )
    assert cnf_path == build_dir / "texmf.cnf"
    content = cnf_path.read_text(encoding="utf-8")
    assert "shell_escape = f" in content
    assert "openin_any = p" in content
    assert "openout_any = p" in content
    assert f"{{{proj_texmf},{glob_texmf}}}" in content

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-123")
    monkeypatch.setenv("WORKSPACE_PASSWORD", "pw-secret-456")
    monkeypatch.setenv("SESSION_SECRET", "sess-secret-789")

    env = build_sanitized_tex_env(
        cnf_dir=build_dir,
        project_texmf_dir=proj_texmf,
        global_texmf_dir=glob_texmf,
        isolated_texmf=False,
    )
    assert env["TEXMFCNF"] == f"{build_dir}:"
    assert env["TEXMFCNF"].endswith(":")
    assert "OPENROUTER_API_KEY" not in env
    assert "WORKSPACE_PASSWORD" not in env
    assert "SESSION_SECRET" not in env

    isolated_cnf = generate_hardened_texmf_cnf(
        build_dir=build_dir,
        project_texmf_dir=proj_texmf,
        global_texmf_dir=glob_texmf,
        isolated_texmf=True,
    )
    assert "TEXMFHOME =" in isolated_cnf.read_text(encoding="utf-8")
    assert str(glob_texmf) not in isolated_cnf.read_text(encoding="utf-8")


def test_staging_rejects_symlinks_outside_project_and_copies_aux_bbl(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "main.tex").write_text(r"\documentclass{article}", encoding="utf-8")

    build_dir = project_dir / ".build"
    build_dir.mkdir()
    (build_dir / "main.aux").write_text(r"\relax ", encoding="utf-8")
    (build_dir / "main.bbl").write_text(r"\begin{thebibliography}{1}\end{thebibliography}", encoding="utf-8")

    staging_dir = tmp_path / "staging_ok"
    stage_compile_workspace(
        project_dir=project_dir,
        build_dir=build_dir,
        staging_dir=staging_dir,
    )
    assert (staging_dir / "main.tex").is_file()
    assert (staging_dir / "main.aux").is_file()
    assert (staging_dir / "main.bbl").is_file()

    # Now add a symlink pointing outside project_dir
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("SECRET_DATA", encoding="utf-8")
    (project_dir / "evil_link.tex").symlink_to(outside_secret)

    staging_bad = tmp_path / "staging_bad"
    with pytest.raises(PermissionError, match="symlink"):
        stage_compile_workspace(
            project_dir=project_dir,
            build_dir=build_dir,
            staging_dir=staging_bad,
        )


@pytest.mark.requires_tex
@pytest.mark.skipif(not HAS_PDFLATEX, reason="TeX Live pdflatex not installed on host")
async def test_malicious_write18_and_input_config_blocked(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir)

    secret_marker = "SUPER_SECRET_OPENROUTER_TOKEN_987654321"
    secrets_file = settings.config_dir / "secrets.yaml"
    secrets_file.write_text(f"openrouter_api_key: {secret_marker}\n", encoding="utf-8")

    pwned_file = tmp_path / "write18_executed.txt"
    project_dir = settings.projects_dir / "malicious_proj"
    project_dir.mkdir(parents=True)
    build_dir = project_dir / ".build"
    build_dir.mkdir()

    tex_source = f"""\\documentclass{{article}}
\\begin{{document}}
Attempting shell execution:
\\immediate\\write18{{echo PWNED > {pwned_file}}}
Attempting secret file read:
\\input{{{secrets_file}}}
\\end{{document}}
"""
    (project_dir / "main.tex").write_text(tex_source, encoding="utf-8")

    result: SandboxResult = await run_sandboxed_tex(
        project_dir=project_dir,
        build_dir=build_dir,
        root_tex="main.tex",
        settings=settings,
    )

    assert not pwned_file.exists(), "\\write18 shell execution must be blocked"
    assert secret_marker not in result.stdout
    assert secret_marker not in result.stderr

    log_file = build_dir / "main.log"
    if log_file.exists():
        assert secret_marker not in log_file.read_text(encoding="utf-8", errors="ignore")

    pdf_file = build_dir / "main.pdf"
    if pdf_file.exists():
        assert secret_marker.encode("utf-8") not in pdf_file.read_bytes()


@pytest.mark.requires_tex
@pytest.mark.skipif(not HAS_PDFLATEX, reason="TeX Live pdflatex not installed on host")
async def test_compile_timeout_kills_runaway_tex_loop(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir)
    settings.compile_timeout_seconds = 2

    project_dir = settings.projects_dir / "runaway_proj"
    project_dir.mkdir(parents=True)
    build_dir = project_dir / ".build"
    build_dir.mkdir()

    (project_dir / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\loop\\iftrue\\repeat\n\\end{document}\n",
        encoding="utf-8",
    )

    result = await run_sandboxed_tex(
        project_dir=project_dir,
        build_dir=build_dir,
        root_tex="main.tex",
        settings=settings,
    )

    assert result.timed_out is True
    assert result.returncode != 0


async def test_run_sandboxed_tex_survives_staging_cleanup_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: files created by the TeX process (other uid / bwrap) in the staging dir
    cannot be deleted by the server; cleanup failure must not turn a finished compile into a 500."""
    import os
    import tempfile

    from app.latex import sandbox

    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir)
    project_dir = settings.projects_dir / "cleanup_proj"
    project_dir.mkdir(parents=True)
    build_dir = project_dir / ".build"
    build_dir.mkdir()
    (project_dir / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n",
        encoding="utf-8",
    )

    undeletable_name = "ecbx1000.600pk"
    undeletable_dir_name = "ec"

    # Keep the intentionally undeletable staging dir inside tmp_path (pytest-managed) instead of system temp.
    stage_root = tmp_path / "stage_root"
    stage_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(stage_root))

    monkeypatch.setattr(sandbox, "probe_bwrap_support", lambda force_reprobe=False: False)

    async def fake_exec_step(cmd, cwd, env, remaining_timeout, memory_limit_mb):
        font_dir = Path(cwd) / ".texlive2026" / "texmf-var" / "fonts" / "pk" / "ljfour" / "jknappen" / undeletable_dir_name
        font_dir.mkdir(parents=True)
        (font_dir / undeletable_name).write_bytes(b"pk")
        return 0, "", "", False

    monkeypatch.setattr(sandbox, "_exec_step", fake_exec_step)

    real_unlink = os.unlink
    real_chmod = os.chmod

    def deny_unlink(path, *args, **kwargs):
        if os.fspath(path).endswith(undeletable_name):
            raise PermissionError(1, "Operation not permitted", os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    def deny_chmod(path, *args, **kwargs):
        # Cleanup's _resetperms runs chmod on the dir (by fd or by path) owned by another uid.
        if isinstance(path, int) or os.fspath(path).endswith(undeletable_dir_name):
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_chmod(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", deny_unlink)
    monkeypatch.setattr(os, "chmod", deny_chmod)

    result = await run_sandboxed_tex(
        project_dir=project_dir,
        build_dir=build_dir,
        root_tex="main.tex",
        settings=settings,
    )

    assert isinstance(result, SandboxResult)
    assert result.returncode == 0
