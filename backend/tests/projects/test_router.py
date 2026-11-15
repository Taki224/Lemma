from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import bootstrap_data_dir
from app.main import create_app


def _make_app(tmp_path: Path):
    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir).model_copy(
        update={"workspace_password": "router-secret-password"}
    )
    return create_app(settings=settings)


def test_unauthenticated_project_routes_and_ws_rejected(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/projects").status_code == 401
        assert client.post("/api/projects", json={"name": "Unauthorized"}).status_code == 401

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/api/projects/demo/ws"):
                pass
        assert exc_info.value.code == 4401

        # Log in to get session cookie, then verify mutation without CSRF token returns 403
        login_res = client.post(
            "/api/auth/login",
            json={"password": "router-secret-password"},
        )
        assert login_res.status_code == 200
        csrf_token = login_res.json()["csrf_token"]

        no_csrf_res = client.post("/api/projects", json={"name": "Missing CSRF"})
        assert no_csrf_res.status_code == 403

        # With CSRF token, project creation succeeds
        create_res = client.post(
            "/api/projects",
            json={"name": "Authenticated Project"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]

        # Authenticated WebSocket connects and sends an initial handshake frame
        with client.websocket_connect(f"/api/projects/{project_id}/ws") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "connected"
            assert msg["project_id"] == project_id


def test_file_save_etag_conflict_returns_409_payload(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    with TestClient(app) as client:
        login_res = client.post(
            "/api/auth/login",
            json={"password": "router-secret-password"},
        )
        assert login_res.status_code == 200
        csrf_token = login_res.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}

        create_res = client.post(
            "/api/projects",
            json={"name": "Concurrency Manuscript"},
            headers=headers,
        )
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]

        # Read initial main.tex and its ETag
        read_res = client.get(f"/api/projects/{project_id}/files/main.tex")
        assert read_res.status_code == 200
        initial_etag = read_res.json()["etag"]

        # First PUT with initial_etag succeeds
        put_1 = client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={
                "content": "\\documentclass{article}\n\\section{First Reveal}\n",
                "expected_etag": initial_etag,
            },
            headers=headers,
        )
        assert put_1.status_code == 200
        updated_etag = put_1.json()["etag"]
        assert updated_etag != initial_etag

        # Verify outline endpoint reflects updated section
        outline_res = client.get(f"/api/projects/{project_id}/outline")
        assert outline_res.status_code == 200
        outline_nodes = outline_res.json()["outline"]
        assert len(outline_nodes) == 1
        assert outline_nodes[0]["title"] == "First Reveal"

        # Second PUT with stale initial_etag returns 409 Conflict with server content & etag
        put_conflict = client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={
                "content": "\\documentclass{article}\n\\section{Conflicting Edit}\n",
                "expected_etag": initial_etag,
            },
            headers=headers,
        )
        assert put_conflict.status_code == 409
        conflict_body = put_conflict.json()
        assert conflict_body["current_etag"] == updated_etag
        assert "First Reveal" in conflict_body["server_content"]


def test_rename_duplicate_and_delete_project_routes(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    with TestClient(app) as client:
        csrf_token = client.post(
            "/api/auth/login",
            json={"password": "router-secret-password"},
        ).json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}
        project_id = client.post("/api/projects", json={"name": "Manage Me"}, headers=headers).json()["project_id"]

        assert client.patch(f"/api/projects/{project_id}", json={"name": "No CSRF"}).status_code == 403

        rename_res = client.patch(f"/api/projects/{project_id}", json={"name": "Managed"}, headers=headers)
        assert rename_res.status_code == 200
        assert rename_res.json()["name"] == "Managed"
        assert client.patch(f"/api/projects/{project_id}", json={"name": " "}, headers=headers).status_code == 400

        dup_res = client.post(f"/api/projects/{project_id}/duplicate", json={}, headers=headers)
        assert dup_res.status_code == 201
        copy_id = dup_res.json()["project_id"]
        assert dup_res.json()["name"] == "Managed (copy)"
        assert client.get(f"/api/projects/{copy_id}/files/main.tex").status_code == 200

        assert client.delete(f"/api/projects/{project_id}", headers=headers).status_code == 204
        assert client.delete(f"/api/projects/{project_id}", headers=headers).status_code == 404
        remaining = [p["project_id"] for p in client.get("/api/projects").json()["projects"]]
        assert remaining == [copy_id]


def test_file_and_folder_management_routes(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    with TestClient(app) as client:
        csrf_token = client.post(
            "/api/auth/login",
            json={"password": "router-secret-password"},
        ).json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}
        pid = client.post("/api/projects", json={"name": "Files"}, headers=headers).json()["project_id"]

        assert client.post(f"/api/projects/{pid}/folders", json={"path": "figs"}).status_code == 403
        assert client.post(f"/api/projects/{pid}/folders", json={"path": "figs"}, headers=headers).status_code == 201
        assert client.post(f"/api/projects/{pid}/folders", json={"path": "figs"}, headers=headers).status_code == 409
        assert "figs" in client.get(f"/api/projects/{pid}/files").json()["folders"]

        move_res = client.post(
            f"/api/projects/{pid}/move",
            json={"from_path": "references.bib", "to_path": "figs/refs.bib"},
            headers=headers,
        )
        assert move_res.status_code == 200
        assert move_res.json()["path"] == "figs/refs.bib"
        assert client.get(f"/api/projects/{pid}/files/figs/refs.bib").status_code == 200

        assert client.delete(f"/api/projects/{pid}/files/figs/refs.bib", headers=headers).status_code == 204
        assert client.delete(f"/api/projects/{pid}/files/figs/refs.bib", headers=headers).status_code == 404
        assert client.delete(f"/api/projects/{pid}/folders/figs", headers=headers).status_code == 204
        assert "figs" not in client.get(f"/api/projects/{pid}/files").json()["folders"]


def test_upload_goes_to_root_or_chosen_folder(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    with TestClient(app) as client:
        csrf_token = client.post(
            "/api/auth/login",
            json={"password": "router-secret-password"},
        ).json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}
        pid = client.post("/api/projects", json={"name": "Uploads"}, headers=headers).json()["project_id"]

        root_res = client.post(
            f"/api/projects/{pid}/uploads",
            files={"file": ("fig.png", b"\x89PNG", "image/png")},
            headers=headers,
        )
        assert root_res.status_code == 201
        assert root_res.json()["path"] == "fig.png"

        nested_res = client.post(
            f"/api/projects/{pid}/uploads",
            files={"file": ("chart.png", b"\x89PNG", "image/png")},
            data={"folder": "figures"},
            headers=headers,
        )
        assert nested_res.status_code == 201
        assert nested_res.json()["path"] == "figures/chart.png"

        bad_res = client.post(
            f"/api/projects/{pid}/uploads",
            files={"file": ("x.png", b"\x89PNG", "image/png")},
            data={"folder": "../escape"},
            headers=headers,
        )
        assert bad_res.status_code == 403
        assert "project-uploads" not in client.get(f"/api/projects/{pid}/files").json()["folders"]


def test_file_save_upload_move_delete_updates_project_index(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    def _indexed_rows(project_id: str) -> dict[str, dict[str, object]]:
        with closing(app.state.db.get_connection()) as conn:
            rows = conn.execute(
                """
                SELECT rel_path, content_hash, line_count, index_json
                FROM project_index_files
                WHERE project_id = ?
                ORDER BY rel_path ASC;
                """,
                (project_id,),
            ).fetchall()
        return {
            str(r["rel_path"]): {
                "content_hash": str(r["content_hash"]),
                "line_count": int(r["line_count"]),
                "index": json.loads(str(r["index_json"])),
            }
            for r in rows
        }

    with TestClient(app) as client:
        csrf_token = client.post(
            "/api/auth/login",
            json={"password": "router-secret-password"},
        ).json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}
        pid = client.post(
            "/api/projects",
            json={"name": "Index Sync Project"},
            headers=headers,
        ).json()["project_id"]

        # 1. Save a .tex file -> indexed in project_index_files
        tex_content = "\\section{Introduction}\n\\label{sec:intro}\nHello world.\n"
        put_res = client.put(
            f"/api/projects/{pid}/files/sections/intro.tex",
            json={"content": tex_content},
            headers=headers,
        )
        assert put_res.status_code == 200

        rows = _indexed_rows(pid)
        assert "sections/intro.tex" in rows
        intro_entities = rows["sections/intro.tex"]["index"]["entities"]  # type: ignore[index]
        assert any(e["name"] == "sec:intro" for e in intro_entities)

        # 2. Upload a .bib file (indexed) and a .png file (not indexed)
        bib_bytes = b"@article{smith2024,\n  title={Great Paper},\n  author={Smith},\n  year={2024}\n}\n"
        bib_res = client.post(
            f"/api/projects/{pid}/uploads",
            files={"file": ("extra.bib", bib_bytes, "text/plain")},
            headers=headers,
        )
        assert bib_res.status_code == 201

        png_res = client.post(
            f"/api/projects/{pid}/uploads",
            files={"file": ("figure.png", b"\x89PNG", "image/png")},
            headers=headers,
        )
        assert png_res.status_code == 201

        rows = _indexed_rows(pid)
        assert "extra.bib" in rows
        assert "figure.png" not in rows
        bib_entities = rows["extra.bib"]["index"]["entities"]  # type: ignore[index]
        assert any(e["name"] == "smith2024" for e in bib_entities)

        # 3. Move/rename .tex file -> old key removed, new key present with updated file_path
        move_res = client.post(
            f"/api/projects/{pid}/move",
            json={"from_path": "sections/intro.tex", "to_path": "sections/background.tex"},
            headers=headers,
        )
        assert move_res.status_code == 200

        rows = _indexed_rows(pid)
        assert "sections/intro.tex" not in rows
        assert "sections/background.tex" in rows
        bg_entities = rows["sections/background.tex"]["index"]["entities"]  # type: ignore[index]
        assert any(
            e["name"] == "sec:intro" and e["file_path"] == "sections/background.tex"
            for e in bg_entities
        )

        # 4. Delete .tex and .bib files -> removed from project_index_files
        assert (
            client.delete(
                f"/api/projects/{pid}/files/sections/background.tex",
                headers=headers,
            ).status_code
            == 204
        )
        assert (
            client.delete(
                f"/api/projects/{pid}/files/extra.bib",
                headers=headers,
            ).status_code
            == 204
        )

        rows = _indexed_rows(pid)
        assert "sections/background.tex" not in rows
        assert "extra.bib" not in rows
