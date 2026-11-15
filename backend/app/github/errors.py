from __future__ import annotations


class GithubApiError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class GithubConflict(Exception):
    """The branch moved between read and update, so the push was not applied."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GithubNotLinked(Exception):
    pass


class GithubSyncBlocked(Exception):
    def __init__(self, code: str, message: str, paths: list[str]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.paths = paths


class GithubSnapshotError(Exception):
    pass
