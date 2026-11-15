from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.github.client import GitHubClient
from app.github.errors import GithubApiError, GithubConflict

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
TREE_A = "c" * 40
TREE_B = "d" * 40
BLOB_CHAPTER = "e" * 40
BLOB_PLOT = "f" * 40
BLOB_README = "1" * 40
BLOB_NEW = "2" * 40
CHAPTER = b"\\section{From GitHub}\n"
PLOT = b"\x89PNG\r\n\x00\xff"
README = b"# Hi"


def test_fetch_maps_a_folder_onto_project_paths_and_commit_keeps_the_rest_of_the_repo() -> None:
    tree_bodies: list[dict] = []
    patch_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token-123"
        assert "test-token-123" not in str(request.url)
        path = request.url.path
        if request.method == "GET" and path == "/repos/acme/manuscript":
            return httpx.Response(200, json={"full_name": "acme/manuscript"})
        if request.method == "GET" and path == "/repos/acme/manuscript/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": COMMIT_A, "type": "commit"}})
        if request.method == "GET" and path == f"/repos/acme/manuscript/git/commits/{COMMIT_A}":
            return httpx.Response(200, json={"sha": COMMIT_A, "tree": {"sha": TREE_A}})
        if request.method == "GET" and path == f"/repos/acme/manuscript/git/trees/{TREE_A}":
            assert request.url.params["recursive"] == "1"
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
                            "path": "paper/figures/plot.png",
                            "mode": "100644",
                            "type": "blob",
                            "sha": BLOB_PLOT,
                            "size": len(PLOT),
                        },
                        {
                            "path": "README.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": BLOB_README,
                            "size": len(README),
                        },
                    ],
                },
            )
        if request.method == "GET" and path == f"/repos/acme/manuscript/git/blobs/{BLOB_CHAPTER}":
            assert "github.raw" in request.headers["Accept"]
            return httpx.Response(200, content=CHAPTER)
        if request.method == "GET" and path == f"/repos/acme/manuscript/git/blobs/{BLOB_PLOT}":
            return httpx.Response(200, content=PLOT)
        if request.method == "GET" and path.endswith(BLOB_README):
            raise AssertionError("files outside the linked folder must not be downloaded")
        if request.method == "POST" and path == "/repos/acme/manuscript/git/blobs":
            body = json.loads(request.content)
            assert base64.b64decode(body["content"]) == b"rewritten"
            assert body["encoding"] == "base64"
            return httpx.Response(201, json={"sha": BLOB_NEW})
        if request.method == "POST" and path == "/repos/acme/manuscript/git/trees":
            body = json.loads(request.content)
            tree_bodies.append(body)
            return httpx.Response(201, json={"sha": TREE_B})
        if request.method == "POST" and path == "/repos/acme/manuscript/git/commits":
            body = json.loads(request.content)
            assert body["parents"] == [COMMIT_A]
            assert body["tree"] == TREE_B
            return httpx.Response(201, json={"sha": COMMIT_B})
        if request.method == "PATCH" and path == "/repos/acme/manuscript/git/refs/heads/main":
            body = json.loads(request.content)
            patch_bodies.append(body)
            assert "force" not in body
            return httpx.Response(200, json={"object": {"sha": COMMIT_B}})
        return httpx.Response(500, json={"message": f"unexpected {request.method} {request.url}"})

    client = GitHubClient("test-token-123", transport=httpx.MockTransport(handler))
    snapshot = _run(client.fetch_snapshot("acme", "manuscript", "main", "paper"))
    assert set(snapshot.files) == {"chapter.tex", "figures/plot.png"}
    assert snapshot.files["figures/plot.png"].content == PLOT
    assert snapshot.commit_sha == COMMIT_A

    new_sha = _run(
        client.commit_folder(
            "acme",
            "manuscript",
            "main",
            "paper",
            snapshot,
            {"chapter.tex": b"rewritten", "figures/plot.png": PLOT},
            "Update chapter",
        )
    )
    assert new_sha == COMMIT_B
    assert tree_bodies[0]["base_tree"] == TREE_A
    paths = [entry["path"] for entry in tree_bodies[0]["tree"]]
    assert paths == ["paper/chapter.tex"]
    assert patch_bodies == [{"sha": COMMIT_B}]


def test_empty_repository_creates_a_branch_without_a_parent() -> None:
    created: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/repos/acme/empty":
            return httpx.Response(200, json={"full_name": "acme/empty"})
        if request.method == "GET" and path == "/repos/acme/empty/git/ref/heads/main":
            return httpx.Response(409, json={"message": "Git Repository is empty."})
        if request.method == "POST" and path == "/repos/acme/empty/git/blobs":
            return httpx.Response(201, json={"sha": BLOB_NEW})
        if request.method == "POST" and path == "/repos/acme/empty/git/trees":
            body = json.loads(request.content)
            assert "base_tree" not in body
            assert body["tree"][0]["path"] == "main.tex"
            return httpx.Response(201, json={"sha": TREE_B})
        if request.method == "POST" and path == "/repos/acme/empty/git/commits":
            body = json.loads(request.content)
            assert "parents" not in body
            return httpx.Response(201, json={"sha": COMMIT_B})
        if request.method == "POST" and path == "/repos/acme/empty/git/refs":
            created.append(json.loads(request.content))
            return httpx.Response(201, json={"ref": "refs/heads/main", "object": {"sha": COMMIT_B}})
        return httpx.Response(500, json={"message": f"unexpected {request.method} {request.url}"})

    client = GitHubClient("test-token-123", transport=httpx.MockTransport(handler))
    snapshot = _run(client.fetch_snapshot("acme", "empty", "main", ""))
    assert snapshot.commit_sha is None
    assert snapshot.files == {}
    new_sha = _run(
        client.commit_folder(
            "acme",
            "empty",
            "main",
            "",
            snapshot,
            {"main.tex": b"hello"},
            "Initial project",
        )
    )
    assert new_sha == COMMIT_B
    assert created == [{"ref": "refs/heads/main", "sha": COMMIT_B}]


def test_rejected_ref_update_is_a_conflict_and_does_not_force() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": BLOB_NEW})
        if request.method == "POST" and request.url.path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": TREE_B})
        if request.method == "POST" and request.url.path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": COMMIT_B})
        if request.method == "PATCH":
            body = json.loads(request.content)
            assert body == {"sha": COMMIT_B}
            return httpx.Response(422, json={"message": "Update is not a fast forward"})
        return httpx.Response(500, json={"message": f"unexpected {request.method} {request.url}"})

    from app.github.client import RemoteFile, RemoteSnapshot

    base = RemoteSnapshot(
        commit_sha=COMMIT_A,
        tree_sha=TREE_A,
        files={"main.tex": RemoteFile("main.tex", "main.tex", BLOB_CHAPTER, b"old")},
    )
    client = GitHubClient("test-token-123", transport=httpx.MockTransport(handler))
    with pytest.raises(GithubConflict):
        _run(client.commit_folder("acme", "manuscript", "main", "", base, {"main.tex": b"new"}, "Update"))


def test_missing_branch_is_not_an_empty_repository() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/repos/acme/manuscript":
            return httpx.Response(200, json={"full_name": "acme/manuscript"})
        if request.url.path.endswith("/git/ref/heads/missing"):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(500, json={"message": f"unexpected {request.method} {request.url}"})

    client = GitHubClient("test-token-123", transport=httpx.MockTransport(handler))
    with pytest.raises(GithubApiError) as error:
        _run(client.fetch_snapshot("acme", "manuscript", "missing", ""))
    assert error.value.status_code == 404


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
