import base64
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.latex.compiler import CompileReport
from app.latex.sandbox import SandboxResult
from app.main import create_app
from app.tools.quick_actions import QuickActionResult
from app.tools.visual_to_latex import VisualLatexResult


@pytest.fixture
def configured_app(tmp_path: Path):
    settings = AppSettings(
        data_dir=tmp_path / "data",
        workspace_password="testpassword",
        privacy_consent_accepted=True,
    )
    app = create_app(settings=settings)
    return app, settings


def _authenticate(client: TestClient, app) -> tuple[dict, str]:
    """Helper to log in and get authenticated cookies and CSRF token."""
    login_res = client.post("/api/auth/login", json={"password": "testpassword"})
    assert login_res.status_code == 200
    cookies = dict(login_res.cookies)
    csrf_token = login_res.json()["csrf_token"]
    return cookies, csrf_token


def test_export_endpoint_streams_verified_zip(configured_app, tmp_path: Path):
    app, settings = configured_app
    with TestClient(app) as client:
        cookies, csrf_token = _authenticate(client, app)

        # Create a project
        create_res = client.post(
            "/api/projects",
            json={"name": "Export Project"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]

        # Save main.tex
        save_res = client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={"content": r"\documentclass{article}\begin{document}Export Test\end{document}"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert save_res.status_code == 200

        project_dir = settings.projects_dir / project_id
        build_dir = project_dir / ".build"
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "main.fls").write_text(f"PWD {project_dir}\nINPUT {project_dir}/main.tex\n")
        (build_dir / "main.bbl").write_text("bbl-content")

        # Mock compiler on app.state
        mock_compiler = MagicMock()
        mock_compiler.compile_project = AsyncMock(
            return_value=CompileReport(
                project_id=project_id,
                root_tex="main.tex",
                status="success",
                returncode=0,
                duration_ms=50,
                sandbox_mode="uid_rlimit",
                fls_path=build_dir / "main.fls",
            )
        )
        app.state.compile_orchestrator = mock_compiler

        # Mock run_sandboxed_tex used during verification
        from unittest.mock import patch
        with patch("app.tools.exporter.run_sandboxed_tex") as mock_tex:
            async def _fake_run_tex(project_dir, build_dir, root_tex, settings, isolated_texmf=False):
                build_dir.mkdir(parents=True, exist_ok=True)
                (build_dir / "main.pdf").write_bytes(b"dummy-pdf")
                return SandboxResult(0, "OK", "", False, "uid_rlimit", 10)
            mock_tex.side_effect = _fake_run_tex

            export_res = client.post(
                f"/api/projects/{project_id}/export",
                json={"root_tex": "main.tex"},
                headers={"X-CSRF-Token": csrf_token},
            )

            assert export_res.status_code == 200
            assert "application/zip" in export_res.headers.get("content-type", "")
            assert export_res.headers.get("x-verification-passed") == "true"

            zf = zipfile.ZipFile(io.BytesIO(export_res.content))
            names = zf.namelist()
            assert "main.tex" in names
            assert "main.bbl" in names


def test_tools_health_check_endpoint(configured_app):
    app, settings = configured_app
    with TestClient(app) as client:
        cookies, csrf_token = _authenticate(client, app)

        create_res = client.post(
            "/api/projects",
            json={"name": "HC Project"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]

        client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={"content": r"\documentclass{article}\begin{document}Test \cite{missing_key}\end{document}"},
            headers={"X-CSRF-Token": csrf_token},
        )

        res = client.post(
            f"/api/projects/{project_id}/tools/health-check",
            json={"root_tex": "main.tex"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert res.status_code == 200
        data = res.json()
        assert "missing_bib_keys" in data
        assert any(k["key"] == "missing_key" for k in data["missing_bib_keys"])


def test_tools_visual_to_latex_csv_endpoint(configured_app):
    app, settings = configured_app
    with TestClient(app) as client:
        cookies, csrf_token = _authenticate(client, app)

        create_res = client.post(
            "/api/projects",
            json={"name": "V2L Project"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]

        res = client.post(
            f"/api/projects/{project_id}/tools/visual-to-latex",
            json={"csv_text": "A,B\n1,2", "caption": "Table 1", "label": "tab:1"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert res.status_code == 200
        data = res.json()
        assert r"\begin{table}" in data["latex_code"]
        assert data["is_safe"] is True


def test_tools_quick_action_endpoint(configured_app):
    app, settings = configured_app
    with TestClient(app) as client:
        cookies, csrf_token = _authenticate(client, app)

        create_res = client.post(
            "/api/projects",
            json={"name": "QA Project"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]

        client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={"content": r"We achieve 95% accuracy \cite{k1}."},
            headers={"X-CSRF-Token": csrf_token},
        )

        res = client.post(
            f"/api/projects/{project_id}/tools/quick-action",
            json={
                "file_path": "main.tex",
                "start_line": 1,
                "end_line": 1,
                "selected_text": r"We achieve 95% accuracy \cite{k1}.",
                "action": "verify_claim",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["action"] == "verify_claim"

        # Traversal check
        traversal_res = client.post(
            f"/api/projects/{project_id}/tools/quick-action",
            json={
                "file_path": "../../../etc/passwd",
                "start_line": 1,
                "end_line": 1,
                "selected_text": "leak",
                "action": "tighten",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert traversal_res.status_code == 400


def test_quick_action_returns_403_for_non_enabled_model(configured_app):
    app, settings = configured_app
    with TestClient(app) as client:
        cookies, csrf_token = _authenticate(client, app)
        headers = {"X-CSRF-Token": csrf_token}

        create_res = client.post("/api/projects", json={"name": "QA 403"}, headers=headers)
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]
        client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={"content": "Wordy sentence here."},
            headers=headers,
        )

        res = client.post(
            f"/api/projects/{project_id}/tools/quick-action",
            json={
                "file_path": "main.tex",
                "start_line": 1,
                "end_line": 1,
                "selected_text": "Wordy sentence here.",
                "action": "tighten",
                "model": "vendor/not-enabled-model",
            },
            headers=headers,
        )

        assert res.status_code == 403
        assert "not enabled" in res.json()["detail"]


def test_visual_to_latex_image_returns_403_for_non_enabled_model(configured_app):
    app, settings = configured_app
    with TestClient(app) as client:
        cookies, csrf_token = _authenticate(client, app)
        headers = {"X-CSRF-Token": csrf_token}

        create_res = client.post("/api/projects", json={"name": "V2L 403"}, headers=headers)
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]

        res = client.post(
            f"/api/projects/{project_id}/tools/visual-to-latex",
            json={
                "image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii"),
                "target_format": "equation",
                "model": "vendor/not-enabled-model",
            },
            headers=headers,
        )

        assert res.status_code == 403
        assert "not enabled" in res.json()["detail"]


def test_quick_action_endpoint_uses_edited_tighten_profile(configured_app):
    app, settings = configured_app
    with TestClient(app) as client:
        cookies, csrf_token = _authenticate(client, app)
        headers = {"X-CSRF-Token": csrf_token}

        create_res = client.post("/api/projects", json={"name": "QA Prompt"}, headers=headers)
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]
        client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={"content": "Wordy sentence here."},
            headers=headers,
        )

        custom_yaml = (
            "profiles:\n"
            "  tighten:\n"
            "    name: Tighten\n"
            "    role: quick_action\n"
            "    system_prompt: CUSTOM-TIGHTEN-MARKER instructions.\n"
        )
        put_res = client.put("/api/ai/prompts", json={"raw_yaml": custom_yaml}, headers=headers)
        assert put_res.status_code == 200
        assert put_res.json()["syntax_warning"] is None

        mock_complete_res = MagicMock()
        mock_complete_res.content = "Short."
        mock_openrouter = MagicMock()
        mock_openrouter.complete = AsyncMock(return_value=mock_complete_res)
        app.state.openrouter_client = mock_openrouter

        res = client.post(
            f"/api/projects/{project_id}/tools/quick-action",
            json={
                "file_path": "main.tex",
                "start_line": 1,
                "end_line": 1,
                "selected_text": "Wordy sentence here.",
                "action": "tighten",
            },
            headers=headers,
        )
        assert res.status_code == 200
        messages = mock_openrouter.complete.call_args.kwargs["messages"]
        system = next(m["content"] for m in messages if m["role"] == "system")
        assert "CUSTOM-TIGHTEN-MARKER" in system


def test_verify_claim_endpoint_unchanged(configured_app):
    app, settings = configured_app
    with TestClient(app) as client:
        cookies, csrf_token = _authenticate(client, app)
        headers = {"X-CSRF-Token": csrf_token}

        create_res = client.post("/api/projects", json={"name": "QA Verify"}, headers=headers)
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]
        client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={"content": r"We achieve 95% accuracy \cite{k1}."},
            headers=headers,
        )

        res = client.post(
            f"/api/projects/{project_id}/tools/quick-action",
            json={
                "file_path": "main.tex",
                "start_line": 1,
                "end_line": 1,
                "selected_text": r"We achieve 95% accuracy \cite{k1}.",
                "action": "verify_claim",
            },
            headers=headers,
        )
        assert res.status_code == 200
        assert res.json()["action"] == "verify_claim"
