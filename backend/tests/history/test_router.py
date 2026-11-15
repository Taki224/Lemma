from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import bootstrap_data_dir
from app.main import create_app


def _login_and_get_csrf(
    client: TestClient, password: str = "history-pass"
) -> dict[str, str]:
    res = client.post("/api/auth/login", json={"password": password})
    assert res.status_code == 200
    csrf = res.json()["csrf_token"]
    return {"X-CSRF-Token": csrf}


def test_history_list_preview_and_restore_over_http(tmp_path: Path) -> None:
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "history-pass",
            "privacy_consent_accepted": True,
        }
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)

        # 1. Create project
        proj_res = client.post(
            "/api/projects",
            json={"name": "History Router Project"},
            headers=headers,
        )
        assert proj_res.status_code == 201
        project_id = proj_res.json()["project_id"]
        project_dir = app.state.project_service.get_project_dir(project_id)

        # 2. Write initial file and take a manual snapshot
        main_tex = project_dir / "main.tex"
        main_tex.write_text("V1 text")
        snap_service = app.state.git_snapshots
        snap1 = snap_service.create_snapshot(
            project_dir=project_dir, trigger="manual", message="Initial draft"
        )
        assert snap1 is not None

        # 3. Write second version with bib file and compile snapshot
        main_tex.write_text("V2 text")
        bib_file = project_dir / "references.bib"
        bib_file.write_text("@article{key1, title={T1}}\n")
        snap2 = snap_service.create_snapshot(
            project_dir=project_dir, trigger="compile_success", message="Second draft"
        )
        assert snap2 is not None

        # 4. GET /api/projects/{project_id}/history
        hist_res = client.get(f"/api/projects/{project_id}/history", headers=headers)
        assert hist_res.status_code == 200
        snapshots = hist_res.json()["snapshots"]
        assert len(snapshots) >= 2
        shas = [s["commit_sha"] for s in snapshots]
        assert snap1.commit_sha in shas
        assert snap2.commit_sha in shas

        # 5. GET /api/projects/{project_id}/history/{sha}/preview
        preview_res = client.get(
            f"/api/projects/{project_id}/history/{snap1.commit_sha}/preview",
            headers=headers,
        )
        assert preview_res.status_code == 200
        preview = preview_res.json()
        assert preview["commit_sha"] == snap1.commit_sha
        assert preview["has_bib_loss_warning"] is True
        assert "key1" in preview["lost_bib_keys"]
        assert "main.tex" in preview["changed_files"]

        # 6. POST /api/projects/{project_id}/history/{sha}/restore requires CSRF
        no_csrf = client.post(
            f"/api/projects/{project_id}/history/{snap1.commit_sha}/restore",
            json={"unsaved_buffers": {}},
        )
        assert no_csrf.status_code == 403

        restore_res = client.post(
            f"/api/projects/{project_id}/history/{snap1.commit_sha}/restore",
            json={"unsaved_buffers": {"notes.tex": "Restoring notes"}},
            headers=headers,
        )
        assert restore_res.status_code == 200
        restored_data = restore_res.json()
        assert "snapshot" in restored_data

        # Verify main.tex restored to V1
        assert main_tex.read_text() == "V1 text"
        # Verify pre_restore commit was created in history
        hist_res_after = client.get(f"/api/projects/{project_id}/history", headers=headers)
        triggers = [s["trigger"] for s in hist_res_after.json()["snapshots"]]
        assert "pre_restore" in triggers
