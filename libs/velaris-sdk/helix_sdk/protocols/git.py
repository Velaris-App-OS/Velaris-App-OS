"""Git backend Protocol — version control for user applications."""
from __future__ import annotations
from typing import Any, Protocol, runtime_checkable
from pydantic import BaseModel


class RepoInfo(BaseModel):
    app_id: str
    name: str
    default_branch: str = "main"
    clone_url_ssh: str | None = None
    clone_url_https: str | None = None

class BranchInfo(BaseModel):
    name: str
    sha: str
    is_default: bool = False

class CommitInfo(BaseModel):
    sha: str
    message: str
    author: str
    timestamp: str
    files_changed: int = 0

class DiffResult(BaseModel):
    files: list[dict[str, Any]] = []
    additions: int = 0
    deletions: int = 0

class MergeResult(BaseModel):
    success: bool
    sha: str | None = None
    conflicts: list[str] = []


@runtime_checkable
class GitBackend(Protocol):
    """Any Git hosting backend — local, GitHub, GitLab, etc."""

    def name(self) -> str: ...
    async def health_check(self) -> bool: ...
    async def initialize(self, config: dict[str, Any]) -> None: ...
    async def shutdown(self) -> None: ...

    # Repository
    async def create_repo(self, app_id: str, name: str) -> RepoInfo: ...
    async def delete_repo(self, app_id: str) -> None: ...
    async def repo_exists(self, app_id: str) -> bool: ...

    # Branches
    async def create_branch(self, app_id: str, branch: str, from_ref: str = "main") -> BranchInfo: ...
    async def delete_branch(self, app_id: str, branch: str) -> None: ...
    async def list_branches(self, app_id: str) -> list[BranchInfo]: ...

    # Commits
    async def commit_files(self, app_id: str, branch: str, files: dict[str, str],
                           message: str, author: str) -> CommitInfo: ...
    async def get_log(self, app_id: str, branch: str, limit: int = 50) -> list[CommitInfo]: ...
    async def get_file(self, app_id: str, branch: str, path: str) -> str | None: ...
    async def list_files(self, app_id: str, branch: str, path: str = "") -> list[str]: ...

    # Diff & Merge
    async def diff(self, app_id: str, base: str, head: str) -> DiffResult: ...
    async def merge(self, app_id: str, source: str, target: str,
                    message: str, strategy: str = "squash") -> MergeResult: ...

    # Tags
    async def create_tag(self, app_id: str, tag: str, ref: str, message: str = "") -> None: ...
    async def list_tags(self, app_id: str) -> list[str]: ...

    # Migration
    async def export_repo(self, app_id: str, target_path: str) -> None: ...
    async def import_repo(self, app_id: str, source_path: str) -> None: ...
