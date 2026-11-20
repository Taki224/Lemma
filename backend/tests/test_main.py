from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import bootstrap_data_dir
from app.latex.compiler import CompileReport
from app.main import create_app


def test_health_endpoint_bootstraps_data_and_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["schema_version"] == 8
        assert payload["sandbox_mode"] in ("bwrap", "uid_rlimit")

    assert (data_dir / "config" / "settings.yaml").is_file()
    assert (data_dir / "app.db").is_file()
    assert (data_dir / "projects").is_dir()
    assert (data_dir / "texmf").is_dir()


@pytest.mark.asyncio
async def test_on_compile_success_and_ai_accept_snapshot_callbacks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = bootstrap_data_dir(tmp_path / "data")
    app = create_app(settings=settings)

    async with app.router.lifespan_context(app):
        project_dir = settings.projects_dir / "test_proj"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "main.tex").write_text(r"\documentclass{article}\begin{document}Hello\end{document}")

        report = CompileReport(
            project_id="test_proj",
            root_tex="main.tex",
            status="success",
            returncode=0,
            duration_ms=42,
            sandbox_mode="bwrap",
            pdf_path=project_dir / ".build" / "main.pdf",
            synctex_path=None,
            attempts=1,
            installed_packages=[],
            missing_packages=[],
        )

        compile_orchestrator = app.state.compile_orchestrator
        git_snapshots = app.state.git_snapshots
        acceptance_coordinator = app.state.acceptance_coordinator

        with caplog.at_level(logging.WARNING):
            await compile_orchestrator.on_compile_success(
                "test_proj", project_dir, report
            )
            for record in caplog.records:
                if record.exc_info and record.exc_info[1]:
                    raise record.exc_info[1].with_traceback(record.exc_info[2])

        snapshots = git_snapshots.list_snapshots(project_dir)
        assert any(s.trigger == "compile_success" for s in snapshots)
        assert snapshots[0].trigger == "compile_success"

        caplog.clear()
        (project_dir / "main.tex").write_text(r"\documentclass{article}\begin{document}Hello World\end{document}")
        with caplog.at_level(logging.WARNING):
            await acceptance_coordinator.on_ai_accept(
                project_dir, "AI accepted citation key [test2026]"
            )
            for record in caplog.records:
                if record.exc_info and record.exc_info[1]:
                    raise record.exc_info[1].with_traceback(record.exc_info[2])

        snapshots = git_snapshots.list_snapshots(project_dir)
        assert any(s.trigger == "ai_accept" for s in snapshots)
        assert snapshots[0].trigger == "ai_accept"


def test_create_app_initializes_and_stops_chat_run_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from app.ai.run_manager import ChatRunManager

    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    app = create_app()
    stop_spy: AsyncMock | None = None
    with TestClient(app):
        assert hasattr(app.state, "chat_run_manager")
        assert isinstance(app.state.chat_run_manager, ChatRunManager)
        original_stop_all = app.state.chat_run_manager.stop_all
        stop_spy = AsyncMock(wraps=original_stop_all)
        monkeypatch.setattr(app.state.chat_run_manager, "stop_all", stop_spy)

    assert stop_spy is not None
    stop_spy.assert_awaited_once()


