from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.github.errors import GithubApiError, GithubConflict
from app.github.paths import (
    MAX_FILE_BYTES,
    MAX_SNAPSHOT_BYTES,
    normalize_sha,
    project_path_from_repo,
    repo_path_for,
)

_BLOB_MODE = "100644"

_API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class RemoteFile:
    repo_path: str
    project_path: str
    blob_sha: str
    content: bytes


@dataclass(frozen=True)
class RemoteSnapshot:
    commit_sha: str | None
    tree_sha: str | None
    files: dict[str, RemoteFile]


class GitHubClient:
    """GitHub Git Data API. This client does not invoke git."""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        cleaned = token.strip()
        if not cleaned:
            raise ValueError("GitHub token is required")
        self._token = cleaned
        self._transport = transport
        self._base_url = base_url.rstrip("/")

    def _open(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "lemma-workspace",
                "X-GitHub-Api-Version": _API_VERSION,
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        )

    def _scrub(self, message: str) -> str:
        text = message.replace(self._token, "[token]").strip()
        return text[:300] or "GitHub request failed"

    def _message(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return self._scrub(message)
        return f"GitHub request failed ({response.status_code})"

    async def _json(
        self,
        http: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        response = await http.request(method, path, json=json_body, params=params, headers=headers)
        if response.status_code >= 300:
            raise GithubApiError(response.status_code, self._message(response))
        if not response.content:
            return {}
        payload = response.json()
        if not isinstance(payload, dict):
            raise GithubApiError(response.status_code, "GitHub returned an unexpected response")
        return payload

    async def fetch_snapshot(self, owner: str, repo: str, branch: str, subpath: str) -> RemoteSnapshot:
        root = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        ref_path = "/".join(quote(part, safe="") for part in branch.split("/"))
        async with self._open() as http:
            await self._json(http, "GET", root)
            ref_response = await http.get(f"{root}/git/ref/heads/{ref_path}")
            # An empty repository has no refs and answers 409. A missing branch is 404.
            if ref_response.status_code == 409:
                return RemoteSnapshot(commit_sha=None, tree_sha=None, files={})
            if ref_response.status_code == 404:
                raise GithubApiError(404, "That GitHub branch was not found.")
            if ref_response.status_code >= 300:
                raise GithubApiError(ref_response.status_code, self._message(ref_response))
            ref_payload = ref_response.json()
            if not isinstance(ref_payload, dict):
                raise GithubApiError(ref_response.status_code, "GitHub returned an unexpected branch")
            commit_sha = normalize_sha(str((ref_payload.get("object") or {}).get("sha") or ""))
            if commit_sha is None:
                raise GithubApiError(502, "GitHub returned an unexpected branch commit")
            commit = await self._json(http, "GET", f"{root}/git/commits/{commit_sha}")
            tree_sha = normalize_sha(str((commit.get("tree") or {}).get("sha") or ""))
            if tree_sha is None:
                raise GithubApiError(502, "GitHub returned an unexpected tree")
            tree = await self._json(
                http,
                "GET",
                f"{root}/git/trees/{tree_sha}",
                params={"recursive": "1"},
            )
            if tree.get("truncated") is True:
                raise GithubApiError(
                    413,
                    "That GitHub tree is too large to sync at once. Link a narrower folder.",
                )
            entries = tree.get("tree")
            if not isinstance(entries, list):
                raise GithubApiError(502, "GitHub returned an unexpected tree")
            files: dict[str, RemoteFile] = {}
            total_bytes = 0
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "blob":
                    continue
                if str(entry.get("mode") or "") == "120000":
                    continue
                repo_path = str(entry.get("path") or "")
                project_path = project_path_from_repo(subpath, repo_path)
                if project_path is None:
                    continue
                size = entry.get("size")
                if isinstance(size, int) and size > MAX_FILE_BYTES:
                    raise GithubApiError(413, f"{repo_path} is larger than 20MB and was not synced.")
                blob_sha = normalize_sha(str(entry.get("sha") or ""))
                if blob_sha is None:
                    raise GithubApiError(502, f"GitHub returned an unexpected blob for {repo_path}")
                blob_response = await http.get(
                    f"{root}/git/blobs/{blob_sha}",
                    headers={"Accept": "application/vnd.github.raw"},
                )
                if blob_response.status_code >= 300:
                    raise GithubApiError(blob_response.status_code, self._message(blob_response))
                content = blob_response.content
                if len(content) > MAX_FILE_BYTES:
                    raise GithubApiError(413, f"{repo_path} is larger than 20MB and was not synced.")
                total_bytes += len(content)
                if total_bytes > MAX_SNAPSHOT_BYTES:
                    raise GithubApiError(413, "That GitHub folder is larger than 100MB and was not synced.")
                files[project_path] = RemoteFile(
                    repo_path=repo_path,
                    project_path=project_path,
                    blob_sha=blob_sha,
                    content=content,
                )
            return RemoteSnapshot(commit_sha=commit_sha, tree_sha=tree_sha, files=files)

    async def commit_folder(
        self,
        owner: str,
        repo: str,
        branch: str,
        subpath: str,
        base: RemoteSnapshot,
        files: dict[str, bytes],
        message: str,
    ) -> str:
        root = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        ref_path = "/".join(quote(part, safe="") for part in branch.split("/"))
        async with self._open() as http:
            entries: list[dict[str, str | None]] = []
            total_bytes = 0
            for project_path in sorted(files):
                content = files[project_path]
                if len(content) > MAX_FILE_BYTES:
                    raise GithubApiError(413, f"{project_path} is larger than 20MB and was not synced.")
                total_bytes += len(content)
                if total_bytes > MAX_SNAPSHOT_BYTES:
                    raise GithubApiError(413, "That project is larger than 100MB and was not synced.")
                remote = base.files.get(project_path)
                if remote is not None and remote.content == content:
                    continue
                blob = await self._json(
                    http,
                    "POST",
                    f"{root}/git/blobs",
                    json_body={
                        "content": base64.b64encode(content).decode("ascii"),
                        "encoding": "base64",
                    },
                )
                blob_sha = normalize_sha(str(blob.get("sha") or ""))
                if blob_sha is None:
                    raise GithubApiError(502, "GitHub returned an unexpected blob id")
                entries.append(
                    {
                        "path": repo_path_for(subpath, project_path),
                        "mode": _BLOB_MODE,
                        "type": "blob",
                        "sha": blob_sha,
                    }
                )
            for project_path, remote in sorted(base.files.items()):
                if project_path in files:
                    continue
                entries.append(
                    {
                        "path": remote.repo_path,
                        "mode": _BLOB_MODE,
                        "type": "blob",
                        "sha": None,
                    }
                )
            if not entries:
                if base.commit_sha is None:
                    raise GithubApiError(400, "There are no project files to push.")
                return base.commit_sha

            tree_body: dict = {"tree": entries}
            if base.tree_sha:
                tree_body["base_tree"] = base.tree_sha
            tree = await self._json(http, "POST", f"{root}/git/trees", json_body=tree_body)
            tree_sha = normalize_sha(str(tree.get("sha") or ""))
            if tree_sha is None:
                raise GithubApiError(502, "GitHub returned an unexpected tree id")
            commit_body: dict = {"message": message, "tree": tree_sha}
            if base.commit_sha:
                commit_body["parents"] = [base.commit_sha]
            commit = await self._json(http, "POST", f"{root}/git/commits", json_body=commit_body)
            new_sha = normalize_sha(str(commit.get("sha") or ""))
            if new_sha is None:
                raise GithubApiError(502, "GitHub returned an unexpected commit id")
            if base.commit_sha:
                ref_response = await http.patch(
                    f"{root}/git/refs/heads/{ref_path}",
                    json={"sha": new_sha},
                )
                if ref_response.status_code == 422:
                    raise GithubConflict(self._message(ref_response))
                if ref_response.status_code >= 300:
                    raise GithubApiError(ref_response.status_code, self._message(ref_response))
            else:
                await self._json(
                    http,
                    "POST",
                    f"{root}/git/refs",
                    json_body={"ref": f"refs/heads/{branch}", "sha": new_sha},
                )
            return new_sha
