from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.config import bootstrap_data_dir
from app.latex.compiler import CompileOrchestrator
from app.latex.synctex import (
    ForwardSyncResult,
    InverseSyncResult,
    SyncTexService,
    _parse_forward_output,
)

HAS_SYNCTEX = (
    shutil.which(
        "synctex",
        path="/Library/TeX/texbin:/usr/local/texlive/bin:/usr/bin:/bin",
    )
    is not None
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "reference_manuscript"


def test_synctex_unmapped_float_returns_fallback_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_dir = tmp_path / ".build"
    build_dir.mkdir()
    (build_dir / "main.pdf").write_bytes(b"%PDF-1.5")

    service = SyncTexService()

    # Simulate synctex view returning no Output: records for line 35, then a fallback record on nearby line 30
    def fake_run_synctex(args: list[str], cwd: Path) -> str:
        cmd_str = " ".join(args)
        if "35:1:" in cmd_str:
            return "This is SyncTeX command line utility, version 1.5\nSyncTeX result begin\nSyncTeX result end\n"
        return (
            "This is SyncTeX command line utility, version 1.5\n"
            "SyncTeX result begin\n"
            "Output:main.pdf\n"
            "Page:2\n"
            "x:72.0\n"
            "y:240.5\n"
            "h:72.0\n"
            "v:252.0\n"
            "W:400.0\n"
            "H:12.0\n"
            "SyncTeX result end\n"
        )

    monkeypatch.setattr(service, "_run_synctex_cli", fake_run_synctex)

    res = service.forward_sync(
        build_dir=build_dir,
        pdf_name="main.pdf",
        tex_file="chapters/methodology.tex",
        line=35,
    )
    assert isinstance(res, ForwardSyncResult)
    assert res.page == 2
    assert res.exact is False
    assert res.hint == "No exact source mapping — jumped to nearest page position"


@pytest.mark.requires_tex
@pytest.mark.skipif(not HAS_SYNCTEX, reason="TeX Live synctex binary not installed on host")
async def test_synctex_forward_and_inverse_accuracy_on_reference_manuscript(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir)
    project_dir = settings.projects_dir / "reference_manuscript"
    shutil.copytree(FIXTURE_DIR, project_dir)

    orchestrator = CompileOrchestrator(settings=settings)
    report = await orchestrator.compile_project(
        project_id="reference_manuscript",
        project_dir=project_dir,
    )
    assert report.status == "success", f"Compile failed: {report.log_excerpt}"
    build_dir = project_dir / ".build"
    assert (build_dir / "main.pdf").is_file()
    assert (build_dir / "main.synctex.gz").is_file()

    service = SyncTexService()

    # 20 sampled prose/section lines across main.tex, chapters/introduction.tex, and chapters/methodology.tex
    sample_targets: list[tuple[str, int]] = [
        ("main.tex", 10),
        ("main.tex", 13),
        ("main.tex", 16),
        ("chapters/introduction.tex", 1),
        ("chapters/introduction.tex", 4),
        ("chapters/introduction.tex", 6),
        ("chapters/introduction.tex", 9),
        ("chapters/introduction.tex", 12),
        ("chapters/introduction.tex", 13),
        ("chapters/introduction.tex", 14),
        ("chapters/introduction.tex", 17),
        ("chapters/introduction.tex", 20),
        ("chapters/introduction.tex", 24),
        ("chapters/introduction.tex", 27),
        ("chapters/methodology.tex", 1),
        ("chapters/methodology.tex", 4),
        ("chapters/methodology.tex", 6),
        ("chapters/methodology.tex", 9),
        ("chapters/methodology.tex", 26),
        ("chapters/methodology.tex", 29),
    ]
    assert len(sample_targets) == 20

    accurate_hits = 0
    for tex_file, target_line in sample_targets:
        fwd = service.forward_sync(
            build_dir=build_dir,
            pdf_name="main.pdf",
            tex_file=tex_file,
            line=target_line,
        )
        assert fwd.page >= 1

        inv: InverseSyncResult = service.inverse_sync(
            build_dir=build_dir,
            pdf_name="main.pdf",
            page=fwd.page,
            x=fwd.x if fwd.x > 0 else fwd.h,
            y=fwd.y if fwd.y > 0 else fwd.v,
        )
        if inv.file_path == tex_file and abs(inv.line - target_line) <= 2:
            accurate_hits += 1

    accuracy = accurate_hits / len(sample_targets)
    assert accuracy >= 0.90, f"SyncTeX roundtrip accuracy {accuracy:.1%} (< 90% SLA; {accurate_hits}/20)"


_VIEW_OUTPUT_WITH_SIZE = (
    "This is SyncTeX command line utility, version 1.5\n"
    "SyncTeX result begin\n"
    "Output:main.pdf\n"
    "Page:1\n"
    "x:171.128296\n"
    "y:134.764618\n"
    "h:133.768356\n"
    "v:134.764618\n"
    "W:343.711060\n"
    "H:6.918498\n"
    "before:\n"
    "offset:-1\n"
    "SyncTeX result end\n"
)

_VIEW_OUTPUT_NO_SIZE = _VIEW_OUTPUT_WITH_SIZE.replace("W:343.711060\n", "").replace("H:6.918498\n", "")


def test_parse_forward_output_reads_width_height() -> None:
    parsed = _parse_forward_output(_VIEW_OUTPUT_WITH_SIZE)
    assert parsed is not None
    assert (parsed.page, parsed.x, parsed.y, parsed.h, parsed.v, parsed.width, parsed.height) == (
        1,
        171.128296,
        134.764618,
        133.768356,
        134.764618,
        343.711060,
        6.918498,
    )


def test_parse_forward_output_missing_size_defaults_zero() -> None:
    parsed = _parse_forward_output(_VIEW_OUTPUT_NO_SIZE)
    assert parsed is not None
    assert parsed.width == 0.0
    assert parsed.height == 0.0


def test_forward_sync_exact_includes_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_dir = tmp_path / ".build"
    build_dir.mkdir()
    service = SyncTexService()
    monkeypatch.setattr(service, "_run_synctex_cli", lambda args, cwd: _VIEW_OUTPUT_WITH_SIZE)

    res = service.forward_sync(build_dir=build_dir, pdf_name="main.pdf", tex_file="main.tex", line=3)

    assert res.exact is True
    assert res.width == pytest.approx(343.711060)
    assert res.height == pytest.approx(6.918498)


def test_forward_sync_fallback_size_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_dir = tmp_path / ".build"
    build_dir.mkdir()
    service = SyncTexService()
    monkeypatch.setattr(service, "_run_synctex_cli", lambda args, cwd: "")

    res = service.forward_sync(build_dir=build_dir, pdf_name="main.pdf", tex_file="main.tex", line=3)

    assert res.exact is False
    assert res.width == 0.0
    assert res.height == 0.0
