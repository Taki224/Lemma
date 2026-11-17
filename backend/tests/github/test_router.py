from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.history.git_snapshots as git_snapshots_module
from app.config import bootstrap_data_dir
from app.history.git_snapshots import GitSnapshotService
from app.main import create_app

COMMIT_A = "a" * 40
COMMIT_C = "c" * 40
TREE_A = "d" * 40
TREE_B = "e" * 40
BLOB_CHAPTER = "f" * 40
BLOB_NEW = "1" * 40
CHAPTER = b"\\section{From GitHub}\n"
_FORBIDDEN_GIT = {"push", "pull", "fetch", "remote", "clone", "ls-remote"}


def test_pull_and_push_use_the_github_api_and_leave_local_version_history_without_a_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    commands: list[list[str]] = []
    real_run = git_snapshots_module.subprocess.run

    def spy(cmd, **kwargs):
        commands.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(git_snapshots_module.subprocess, "run", spy)

    state = {"head": COMMIT_A, "tree_body": None, "patch_body": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/repos/acme/manuscript":
            return httpx.Response(200, json={"full_name": "acme/manuscript"})
        if request.method == "GET" and path == "/repos/acme/manuscript/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": state["head"], "type": "commit"}})
        if request.method == "GET" and path.startswith("/repos/acme/manuscript/git/commits/"):
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"sha": sha, "tree": {"sha": TREE_A}})
        if request.method == "GET" and path == f"/repos/acme/manuscript/git/trees/{TREE_A}":
            return httpx.Response(
                200,
                json={
                    "sha": TREE_A,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "paper/chapter.tex",
                            "mode": "100644",
                            "type": "blob",
                            "sha": BLOB_CHAPTER,
                            "size": len(CHAPTER),
                        },
                        {
                            "path": "README.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "2" * 40,
                            "size": 3,
                        },
                    ],
                },
            )
        if request.method == "GET" and path == f"/repos/acme/manuscript/git/blobs/{BLOB_CHAPTER}":
            return httpx.Response(200, content=CHAPTER)
        if request.method == "POST" and path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": BLOB_NEW})
        if request.method == "POST" and path.endswith("/git/trees"):
            state["tree_body"] = json.loads(request.content)
            return httpx.Response(201, json={"sha": TREE_B})
        if request.method == "POST" and path.endswith("/git/commits"):
            body = json.loads(request.content)
            assert body["parents"] == [COMMIT_A]
            return httpx.Response(201, json={"sha": "3" * 40})
        if request.method == "PATCH" and path.endswith("/git/refs/heads/main"):
            state["patch_body"] = json.loads(request.content)
            assert "force" not in state["patch_body"]
            return httpx.Response(200, json={"object": {"sha": state["patch_body"]["sha"]}})
        return httpx.Response(500, json={"message": f"unexpected {request.method} {request.url}"})

    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={"workspace_password": "github-router-pass", "privacy_consent_accepted": True}
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        app.state.github_transport = httpx.MockTransport(handler)
        denied = client.get("/api/projects/missing/github")
        assert denied.status_code == 401

        login = client.post("/api/auth/login", json={"password": "github-router-pass"})
        assert login.status_code == 200
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        created = client.post("/api/projects", json={"name": "Manuscript"}, headers=headers)
        assert created.status_code == 201
        project_id = created.json()["project_id"]

        rejected = client.put(
            f"/api/projects/{project_id}/github",
            json={"owner": "acme", "repo": "manuscript", "branch": "main", "subpath": "../secret"},
            headers=headers,
        )
        assert rejected.status_code == 400

        linked = client.put(
            f"/api/projects/{project_id}/github",
            json={
                "owner": "acme",
                "repo": "manuscript",
                "branch": "main",
                "subpath": "paper",
                "token": "ghp_test_token_value",
            },
            headers=headers,
        )
        assert linked.status_code == 200
        assert linked.json()["linked"] is True
        assert linked.json()["subpath"] == "paper"
        assert linked.json()["token_source"] == "saved"
        assert "ghp_test_token_value" not in linked.text
        record = logging.LogRecord("github-test", logging.INFO, __file__, 1, "token ghp_test_token_value", (), None)
        assert app.state.secret_redactor.filter(record) is True
        assert "ghp_test_token_value" not in record.getMessage()

        pulled = client.post(
            f"/api/projects/{project_id}/github/pull",
            json={"confirm_overwrite": False},
            headers=headers,
        )
        assert pulled.status_code == 200, pulled.text
        assert pulled.json()["added"] == ["chapter.tex"]
        chapter = client.get(f"/api/projects/{project_id}/files/chapter.tex")
        assert chapter.status_code == 200
        assert "From GitHub" in chapter.json()["content"]
        assert client.get(f"/api/projects/{project_id}/files/main.tex").status_code == 404

        project_dir = settings.data_dir / "projects" / project_id
        config = (project_dir / ".git" / "config").read_text(encoding="utf-8")
        assert "[remote" not in config
        snapshots = GitSnapshotService().list_snapshots(project_dir)
        messages = [item.message for item in snapshots]
        assert "Before pull from github.com/acme/manuscript/paper (main)" in messages
        assert "Pulled from github.com/acme/manuscript/paper (main)" in messages

        saved = client.put(
            f"/api/projects/{project_id}/files/chapter.tex",
            json={"content": "rewritten chapter"},
            headers=headers,
        )
        assert saved.status_code == 200
        state["head"] = COMMIT_C
        moved = client.post(
            f"/api/projects/{project_id}/github/push",
            json={"message": "Update chapter", "confirm_overwrite": True},
            headers=headers,
        )
        assert moved.status_code == 409
        assert moved.json()["detail"]["code"] == "remote_moved"
        assert state["tree_body"] is None

        state["head"] = COMMIT_A
        pushed = client.post(
            f"/api/projects/{project_id}/github/push",
            json={"message": "Update chapter"},
            headers=headers,
        )
        assert pushed.status_code == 200, pushed.text
        assert state["tree_body"]["base_tree"] == TREE_A
        assert [entry["path"] for entry in state["tree_body"]["tree"]] == ["paper/chapter.tex"]
        assert state["patch_body"] == {"sha": "3" * 40}

    assert commands
    assert any("commit" in cmd for cmd in commands)
    for cmd in commands:
        assert _FORBIDDEN_GIT.isdisjoint(cmd)


def test_settings_keys_and_github_link_by_token_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={"workspace_password": "github-router-pass", "privacy_consent_accepted": True}
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"password": "github-router-pass"})
        assert login.status_code == 200
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}

        created = client.post("/api/projects", json={"name": "Named Token Proj"}, headers=headers)
        assert created.status_code == 201
        project_id = created.json()["project_id"]

        # Add a named GitHub key in Settings
        add_res = client.post(
            "/api/settings/keys",
            json={"provider": "github", "name": "work-key", "key": "ghp_work_secret_token"},
            headers=headers,
        )
        assert add_res.status_code == 201
        assert add_res.json()["github"] == [{"name": "work-key"}]
        assert "ghp_work_secret_token" not in add_res.text

        # Link repository selecting the key by name
        linked = client.put(
            f"/api/projects/{project_id}/github",
            json={
                "owner": "acme",
                "repo": "manuscript",
                "branch": "main",
                "subpath": "",
                "token_name": "work-key",
            },
            headers=headers,
        )
        assert linked.status_code == 200
        assert linked.json()["linked"] is True
        assert linked.json()["has_token"] is True
        assert linked.json()["token_name"] == "work-key"

        # Selecting a non-existent key name fails with 400
        missing_key = client.put(
            f"/api/projects/{project_id}/github",
            json={
                "owner": "acme",
                "repo": "manuscript",
                "branch": "main",
                "subpath": "",
                "token_name": "nonexistent",
            },
            headers=headers,
        )
        assert missing_key.status_code == 400

