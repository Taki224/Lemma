from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db import DatabaseManager
from app.github.paths import normalize_sha


@dataclass(frozen=True)
class GithubLink:
    project_id: str
    owner: str
    repo: str
    branch: str
    subpath: str
    last_synced_sha: str | None
    updated_at: str


class GithubLinkStore:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def get(self, project_id: str) -> GithubLink | None:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute(
                """
                SELECT project_id, owner, repo, branch, subpath, last_synced_sha, updated_at
                FROM github_links
                WHERE project_id = ?;
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return GithubLink(
            project_id=str(row["project_id"]),
            owner=str(row["owner"]),
            repo=str(row["repo"]),
            branch=str(row["branch"]),
            subpath=str(row["subpath"]),
            last_synced_sha=str(row["last_synced_sha"]) if row["last_synced_sha"] else None,
            updated_at=str(row["updated_at"]),
        )

    def upsert(self, project_id: str, owner: str, repo: str, branch: str, subpath: str) -> GithubLink:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                """
                INSERT INTO github_links (
                    project_id, owner, repo, branch, subpath, last_synced_sha, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    owner = excluded.owner,
                    repo = excluded.repo,
                    branch = excluded.branch,
                    subpath = excluded.subpath,
                    last_synced_sha = CASE
                        WHEN github_links.owner = excluded.owner
                         AND github_links.repo = excluded.repo
                         AND github_links.branch = excluded.branch
                         AND github_links.subpath = excluded.subpath
                        THEN github_links.last_synced_sha
                        ELSE NULL
                    END,
                    updated_at = excluded.updated_at;
                """,
                (project_id, owner, repo, branch, subpath, now),
            )
        link = self.get(project_id)
        if link is None:
            raise RuntimeError("GitHub link disappeared after save")
        return link

    def mark_synced(self, project_id: str, sha: str | None) -> None:
        clean = normalize_sha(sha)
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.db.get_connection()) as conn:
            cursor = conn.execute(
                """
                UPDATE github_links
                SET last_synced_sha = ?, updated_at = ?
                WHERE project_id = ?;
                """,
                (clean, now, project_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(project_id)

    def delete(self, project_id: str) -> bool:
        with closing(self.db.get_connection()) as conn:
            cursor = conn.execute(
                "DELETE FROM github_links WHERE project_id = ?;",
                (project_id,),
            )
            return cursor.rowcount > 0
