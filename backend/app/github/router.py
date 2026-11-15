from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.session import SessionRecord, require_authenticated_session, require_csrf
from app.config_secrets import SecretsStore
from app.github.client import GitHubClient
from app.github.errors import (
    GithubApiError,
    GithubConflict,
    GithubNotLinked,
    GithubSnapshotError,
    GithubSyncBlocked,
)
from app.github.paths import normalize_branch, normalize_owner, normalize_repo, normalize_subpath
from app.github.store import GithubLink, GithubLinkStore
from app.github.sync import GithubSync, SyncOutcome
from app.history.git_snapshots import GitSnapshotService
from app.projects.service import ProjectService

logger = logging.getLogger(__name__)

github_router = APIRouter(tags=["github"])


class LinkGithubRequest(BaseModel):
    owner: str
    repo: str
    branch: str = "main"
    subpath: str = ""
    token: str | None = None
    clear_token: bool = False


class PullGithubRequest(BaseModel):
    confirm_overwrite: bool = False


class PushGithubRequest(BaseModel):
    message: str = "Update project files"
    confirm_overwrite: bool = False


def _store(request: Request) -> GithubLinkStore:
    return GithubLinkStore(request.app.state.db)


def _projects(request: Request) -> ProjectService:
    return request.app.state.project_service


def _secrets(request: Request) -> SecretsStore:
    return request.app.state.secrets_store


def _clean_token(raw: str | None) -> str | None:
    if raw is None:
        return None
    token = raw.strip()
    if not token:
        return None
    if any(char.isspace() for char in token) or len(token) < 8 or len(token) > 512:
        raise ValueError("GitHub token must be a single token of 8 to 512 characters.")
    return token


def _client(request: Request) -> GitHubClient:
    token = _secrets(request).get("github_token")
    if not token:
        raise HTTPException(status_code=400, detail="Add a GitHub token before syncing.")
    transport: httpx.AsyncBaseTransport | None = getattr(request.app.state, "github_transport", None)
    return GitHubClient(token=token, transport=transport)


def _token_state(secrets: SecretsStore) -> tuple[bool, str | None]:
    source = secrets.secret_source("github_token")
    return source is not None, source


def _public_link(link: GithubLink | None, has_token: bool, token_source: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"linked": link is not None, "has_token": has_token, "token_source": token_source}
    if link is None:
        return payload
    payload.update(
        {
            "owner": link.owner,
            "repo": link.repo,
            "branch": link.branch,
            "subpath": link.subpath,
            "last_synced_sha": link.last_synced_sha,
        }
    )
    return payload


def _outcome(result: SyncOutcome) -> dict[str, Any]:
    return {
        "commit_sha": result.commit_sha,
        "added": list(result.added),
        "updated": list(result.updated),
        "deleted": list(result.deleted),
        "unchanged": result.unchanged,
        "snapshot_warning": result.snapshot_warning,
    }


def _require_project(service: ProjectService, project_id: str) -> None:
    try:
        service.get_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _api_error(exc: GithubApiError) -> HTTPException:
    logger.warning("GitHub API error %s: %s", exc.status_code, exc.message)
    if exc.status_code in {401, 403}:
        return HTTPException(
            status_code=403,
            detail="GitHub rejected the token or it cannot access that repository.",
        )
    if exc.status_code == 404:
        return HTTPException(status_code=404, detail="That GitHub repository or branch was not found.")
    if exc.status_code in {400, 413, 422}:
        return HTTPException(status_code=400, detail=exc.message)
    return HTTPException(status_code=502, detail=exc.message or "GitHub request failed.")


def _blocked(exc: GithubSyncBlocked) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": exc.code, "message": exc.message, "paths": exc.paths},
    )


def _raise_mapped(exc: Exception, conflict_message: str) -> None:
    if isinstance(exc, GithubNotLinked):
        raise HTTPException(status_code=404, detail="This project is not linked to GitHub.") from exc
    if isinstance(exc, GithubSyncBlocked):
        raise _blocked(exc) from exc
    if isinstance(exc, GithubSnapshotError):
        raise HTTPException(
            status_code=500,
            detail="Could not save a local version snapshot, so the pull was stopped.",
        ) from exc
    if isinstance(exc, GithubConflict):
        raise HTTPException(
            status_code=409,
            detail={"code": "remote_moved", "message": conflict_message, "paths": []},
        ) from exc
    if isinstance(exc, GithubApiError):
        raise _api_error(exc) from exc
    if isinstance(exc, httpx.HTTPError):
        raise HTTPException(status_code=502, detail="Could not reach GitHub.") from exc
    raise exc


def _local_snapshot(git_snapshots: GitSnapshotService, project_dir: Path, message: str) -> None:
    """Record the working tree in local version history. Does not set a remote."""
    try:
        git_snapshots.create_snapshot(project_dir=project_dir, trigger="manual", message=message)
    except Exception as exc:
        raise GithubSnapshotError(str(exc)) from exc


@github_router.get("/api/projects/{project_id}/github")
async def get_github_link(
    project_id: str,
    request: Request,
    _session: SessionRecord = Depends(require_authenticated_session),
) -> dict[str, Any]:
    service = _projects(request)
    _require_project(service, project_id)
    link = _store(request).get(project_id)
    has_token, token_source = _token_state(_secrets(request))
    return _public_link(link, has_token, token_source)


@github_router.put("/api/projects/{project_id}/github")
async def link_github_repo(
    project_id: str,
    body: LinkGithubRequest,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _projects(request)
    _require_project(service, project_id)
    secrets = _secrets(request)
    if body.clear_token and not (body.token or "").strip() and not body.owner.strip() and not body.repo.strip():
        secrets.update_secrets({"github_token": None})
        has_token, token_source = _token_state(secrets)
        return _public_link(_store(request).get(project_id), has_token, token_source)
    try:
        owner = normalize_owner(body.owner)
        repo = normalize_repo(body.repo)
        branch = normalize_branch(body.branch)
        subpath = normalize_subpath(body.subpath)
        token = _clean_token(body.token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if token:
        secrets.update_secrets({"github_token": token})
        redactor = getattr(request.app.state, "secret_redactor", None)
        if redactor is not None:
            redactor.register_secrets([token])
    elif body.clear_token:
        secrets.update_secrets({"github_token": None})
    link = _store(request).upsert(project_id, owner, repo, branch, subpath)
    has_token, token_source = _token_state(secrets)
    return _public_link(link, has_token, token_source)


@github_router.delete("/api/projects/{project_id}/github")
async def unlink_github_repo(
    project_id: str,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _projects(request)
    _require_project(service, project_id)
    _store(request).delete(project_id)
    has_token, token_source = _token_state(_secrets(request))
    return _public_link(None, has_token, token_source)


@github_router.post("/api/projects/{project_id}/github/pull")
async def pull_github_repo(
    project_id: str,
    body: PullGithubRequest,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _projects(request)
    _require_project(service, project_id)
    git_snapshots: GitSnapshotService = request.app.state.git_snapshots
    sync = GithubSync(service, _store(request), _client(request))

    def snapshot(project_dir: Path, message: str) -> None:
        _local_snapshot(git_snapshots, project_dir, message)

    try:
        result = await sync.pull(
            project_id,
            confirm_overwrite=body.confirm_overwrite,
            snapshot=snapshot,
            compile_lock=request.app.state.compile_orchestrator.get_lock(project_id),
        )
    except Exception as exc:
        _raise_mapped(exc, "The GitHub branch changed during the pull. Try again.")
    return _outcome(result)


@github_router.post("/api/projects/{project_id}/github/push")
async def push_github_repo(
    project_id: str,
    body: PushGithubRequest,
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    service = _projects(request)
    _require_project(service, project_id)
    sync = GithubSync(service, _store(request), _client(request))
    try:
        result = await sync.push(
            project_id,
            message=body.message,
            confirm_overwrite=body.confirm_overwrite,
            compile_lock=request.app.state.compile_orchestrator.get_lock(project_id),
        )
    except Exception as exc:
        _raise_mapped(exc, "The GitHub branch changed while pushing. Pull and try again.")
    return _outcome(result)
