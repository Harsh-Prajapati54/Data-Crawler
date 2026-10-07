from __future__ import annotations

from typing import Any

from .config import RepositoryFilterConfig


def repository_passes_filters(repo: dict[str, Any], config: RepositoryFilterConfig) -> tuple[bool, str]:
    if config.exclude_forks and repo.get("fork", False):
        return False, "fork"
    if config.exclude_archived and repo.get("archived", False):
        return False, "archived"
    if repo.get("stargazers_count", 0) < config.min_stars:
        return False, "stars"
    # GitHub reports repository size in KB. The user-facing config is bytes.
    # Convert the reported KB value to bytes before comparing.
    repository_size_bytes = int(repo.get("size", 0) or 0) * 1024
    if repository_size_bytes > config.max_size_bytes:
        return False, "repository_size"

    license_info = repo.get("license")
    if config.require_license and not license_info:
        return False, "missing_license"

    if config.require_license:
        license_id = (license_info or {}).get("spdx_id")
        if license_id not in config.allowed_licenses:
            return False, f"license:{license_id or 'unknown'}"

    return True, "accepted"


def repo_identity(repo: dict[str, Any]) -> str:
    return repo.get("full_name") or f"{repo.get('owner', {}).get('login')}/{repo.get('name')}"
