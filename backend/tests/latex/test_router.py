from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import bootstrap_data_dir
from app.main import create_app

HAS_PDFLATEX = (
    shutil.which(
        "pdflatex",
        path="/Library/TeX/texbin:/usr/local/texlive/bin:/usr/bin:/bin",
    )
    is not None
)


@pytest.mark.requires_tex
@pytest.mark.skipif(not HAS_PDFLATEX, reason="TeX Live pdflatex not installed on host")
def test_compile_endpoint_and_synctex_roundtrip(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir).model_copy(
        update={"workspace_password": "latex-router-password"}
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        login_res = client.post(
            "/api/auth/login",
            json={"password": "latex-router-password"},
        )
        assert login_res.status_code == 200
        csrf_token = login_res.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}

        create_res = client.post(
            "/api/projects",
            json={"name": "Compile Router Study"},
            headers=headers,
        )
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]

        # Trigger compile
        compile_res = client.post(
            f"/api/projects/{project_id}/compile",
            headers=headers,
        )
        assert compile_res.status_code == 200
        compile_data = compile_res.json()
        assert compile_data["status"] == "success"
        assert compile_data["pdf_url"] == f"/api/projects/{project_id}/pdf"
        assert isinstance(compile_data["diagnostics"], list)

        # Fetch compiled PDF bytes with Cache-Control: no-store
        pdf_res = client.get(f"/api/projects/{project_id}/pdf")
        assert pdf_res.status_code == 200
        assert pdf_res.headers.get("cache-control") == "no-store"
        assert pdf_res.content.startswith(b"%PDF-")

        # Forward SyncTeX from main.tex line 10
        fwd_res = client.get(
            f"/api/projects/{project_id}/synctex/forward",
            params={"file": "main.tex", "line": 10},
        )
        assert fwd_res.status_code == 200
        fwd_data = fwd_res.json()
        assert fwd_data["page"] >= 1

        # Inverse SyncTeX from those coordinates
        inv_res = client.get(
            f"/api/projects/{project_id}/synctex/inverse",
            params={
                "page": fwd_data["page"],
                "x": fwd_data["x"],
                "y": fwd_data["y"],
            },
        )
        assert inv_res.status_code == 200
        inv_data = inv_res.json()
        assert inv_data["file_path"] == "main.tex"
        assert inv_data["line"] >= 1

        # Manual .sty upload into per-project .texmf
        upload_res = client.post(
            f"/api/projects/{project_id}/texmf/upload",
            files={"file": ("custommacro.sty", b"\\ProvidesPackage{custommacro}\n", "text/plain")},
            headers=headers,
        )
        assert upload_res.status_code == 201
        assert upload_res.json()["filename"] == "custommacro.sty"


@pytest.mark.requires_tex
@pytest.mark.skipif(not HAS_PDFLATEX, reason="TeX Live pdflatex not installed on host")
def test_forward_endpoint_returns_width_height(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir).model_copy(
        update={"workspace_password": "latex-router-password"}
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        login_res = client.post("/api/auth/login", json={"password": "latex-router-password"})
        assert login_res.status_code == 200
        headers = {"X-CSRF-Token": login_res.json()["csrf_token"]}

        create_res = client.post("/api/projects", json={"name": "Forward Size"}, headers=headers)
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]

        compile_res = client.post(f"/api/projects/{project_id}/compile", headers=headers)
        assert compile_res.status_code == 200
        assert compile_res.json()["status"] == "success"

        fwd_res = client.get(
            f"/api/projects/{project_id}/synctex/forward",
            params={"file": "main.tex", "line": 10},
        )
        assert fwd_res.status_code == 200
        data = fwd_res.json()
        assert isinstance(data["width"], float)
        assert isinstance(data["height"], float)
        assert data["width"] > 0.0
        assert data["height"] > 0.0


def test_project_tex_engine_setting_and_compile_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.latex.compiler import CompileReport

    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir).model_copy(
        update={"workspace_password": "latex-router-password"}
    )
    app = create_app(settings=settings)

    captured_engines: list[str] = []

    async def fake_compile_project(
        project_id: str,
        project_dir: Path,
        root_tex: str | None = None,
        tex_engine: str = "pdflatex",
    ) -> CompileReport:
        captured_engines.append(tex_engine)
        return CompileReport(
            project_id=project_id,
            root_tex=root_tex or "main.tex",
            status="success",
            returncode=0,
            duration_ms=10,
            sandbox_mode="uid_rlimit",
        )

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.compile_orchestrator, "compile_project", fake_compile_project)

        login_res = client.post("/api/auth/login", json={"password": "latex-router-password"})
        assert login_res.status_code == 200
        headers = {"X-CSRF-Token": login_res.json()["csrf_token"]}

        create_res = client.post("/api/projects", json={"name": "Engine Test"}, headers=headers)
        assert create_res.status_code == 201
        project = create_res.json()
        assert project["tex_engine"] == "pdflatex"
        project_id = project["project_id"]

        bad_res = client.patch(
            f"/api/projects/{project_id}/settings",
            json={"tex_engine": "bogustex"},
            headers=headers,
        )
        assert bad_res.status_code == 400

        patch_res = client.patch(
            f"/api/projects/{project_id}/settings",
            json={"tex_engine": "xelatex"},
            headers=headers,
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["tex_engine"] == "xelatex"

        compile_res = client.post(f"/api/projects/{project_id}/compile", headers=headers)
        assert compile_res.status_code == 200
        assert compile_res.json()["status"] == "success"
        assert captured_engines == ["xelatex"]
