from __future__ import annotations

import base64
import time
from typing import Any

import requests

from .config import GitHubConfig


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub API operation cannot be completed."""


class GitHubClient:
    def __init__(self, config: GitHubConfig, token: str | None, logger):
        self.config = config
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": config.api_version,
                "User-Agent": "github-code-crawler/0.2.0",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        self.session.close()

    def _url(self, path: str) -> str:
        return f"{self.config.api_base_url}/{path.lstrip('/')}"

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    self._url(path),
                    params=params,
                    timeout=self.config.timeout_seconds,
                )

                remaining = response.headers.get("X-RateLimit-Remaining")
                if remaining is not None:
                    self.logger.debug(
                        "GitHub %s %s -> %s (rate remaining=%s)",
                        method,
                        path,
                        response.status_code,
                        remaining,
                    )

                if response.ok:
                    return response.json()

                retryable = (
                    response.status_code in {403, 429}
                    or 500 <= response.status_code < 600
                )
                if not retryable or attempt >= self.config.max_retries:
                    detail = response.text[:500]
                    raise GitHubAPIError(
                        f"GitHub API {response.status_code} for {method} {path}: {detail}"
                    )

                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    delay = float(retry_after)
                elif (
                    response.headers.get("X-RateLimit-Remaining") == "0"
                    and response.headers.get("X-RateLimit-Reset")
                ):
                    reset_at = int(response.headers["X-RateLimit-Reset"])
                    delay = max(1, reset_at - int(time.time()))
                else:
                    delay = self.config.retry_backoff_seconds * (2**attempt)

                self.logger.warning(
                    "GitHub request retry %d/%d after HTTP %s; sleeping %.1fs: %s",
                    attempt + 1,
                    self.config.max_retries,
                    response.status_code,
                    delay,
                    path,
                )
                time.sleep(delay)

            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                delay = self.config.retry_backoff_seconds * (2**attempt)
                self.logger.warning(
                    "Request retry %d/%d after %s; sleeping %.1fs: %s",
                    attempt + 1,
                    self.config.max_retries,
                    type(exc).__name__,
                    delay,
                    path,
                )
                time.sleep(delay)

        raise GitHubAPIError(
            f"GitHub request failed for {method} {path}: {last_error}"
        )

    def search_repositories(
        self,
        query: str,
        max_repositories: int,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        repositories: list[dict[str, Any]] = []
        page = max(1, start_page)
        page_limit = page + (max_pages - 1 if max_pages else 10**9)
        exhausted = False

        while len(repositories) < max_repositories and page <= page_limit:
            remaining = max_repositories - len(repositories)
            per_page = min(self.config.page_size, remaining, 100)
            data = self.request_json(
                "GET",
                "/search/repositories",
                params={
                    "q": query,
                    "sort": self.config.sort,
                    "order": self.config.order,
                    "per_page": per_page,
                    "page": page,
                },
            )
            items = data.get("items", [])
            repositories.extend(items)
            if not items or len(items) < per_page:
                exhausted = True
                break
            page += 1

        return repositories[:max_repositories], page, exhausted

    def get_tree(
        self,
        owner: str,
        repo: str,
        tree_ref: str,
        recursive: bool = True,
    ) -> dict[str, Any]:
        params = {"recursive": "1"} if recursive else None
        return self.request_json(
            "GET",
            f"/repos/{owner}/{repo}/git/trees/{tree_ref}",
            params=params,
        )

    def get_blob(self, owner: str, repo: str, sha: str) -> bytes:
        data = self.request_json(
            "GET",
            f"/repos/{owner}/{repo}/git/blobs/{sha}",
        )
        if data.get("encoding") != "base64":
            raise GitHubAPIError(
                f"Unexpected blob encoding for {owner}/{repo}:{sha}"
            )
        return base64.b64decode(data.get("content", ""))
