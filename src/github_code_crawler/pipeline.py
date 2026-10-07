from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .code_processing import RejectedCode, process_code
from .config import AppConfig, SearchQueryConfig
from .dataset import (
    append_jsonl,
    jsonl_record_size_bytes,
    reset_outputs,
    total_dataset_size_bytes,
)
from .github_client import GitHubAPIError, GitHubClient
from .repository import repo_identity, repository_passes_filters
from .sampler import sample_repositories
from .security import scan_for_secrets
from .state import CrawlState


QUALITY_REASONS = (
    "syntax_error",
    "tokenization_error",
    "generated_code",
    "high_line_repetition",
    "too_few_tokens",
)


class CrawlerPipeline:
    def __init__(self, config: AppConfig, logger, client: GitHubClient):
        self.config = config
        self.logger = logger
        self.client = client
        self.stats: dict[str, Any] = {
            "queries": 0,
            "candidate_results": 0,
            "unique_candidates": 0,
            "repositories_found": 0,
            "repositories_accepted": 0,
            "repositories_rejected": 0,
            "repositories_selected": 0,
            "repositories_skipped_previously_crawled": 0,
            "repositories_crawled": 0,
            "repositories_failed": 0,
            "files_found": 0,
            "files_attempted": 0,
            "files_accepted": 0,
            "rejected_cleaning": 0,
            "rejected_quality": 0,
            "rejected_security": 0,
            "rejected_exact_duplicate": 0,
            "rejected_structural_duplicate": 0,
            "skipped_large_file": 0,
            "errors": 0,
            "storage_limit_reached": False,
            "dataset_size_bytes": 0,
            "storage_limit_reached": False,
        }

    def _ignored_path(self, path: str) -> bool:
        ignored = self.config.file_filter.ignore_directories
        parts = {part.lower() for part in Path(path).parts[:-1]}
        return bool(parts & ignored)

    def _eligible_files(self, tree: dict[str, Any]) -> list[dict[str, Any]]:
        extensions = self.config.file_filter.extensions
        result: list[dict[str, Any]] = []
        for item in tree.get("tree", []):
            path = item.get("path", "")
            if item.get("type") != "blob":
                continue
            if self._ignored_path(path):
                continue
            if not any(path.lower().endswith(ext) for ext in extensions):
                continue
            result.append(item)
        return result

    def _get_tree_entries(self, repo: dict[str, Any]) -> dict[str, Any]:
        owner = repo["owner"]["login"]
        name = repo["name"]
        branch = repo["default_branch"]
        tree = self.client.get_tree(owner, name, branch, recursive=True)
        if not tree.get("truncated"):
            return tree

        self.logger.warning(
            "Tree truncated for %s; falling back to subtree walk",
            repo_identity(repo),
        )
        root = self.client.get_tree(owner, name, branch, recursive=False)
        entries: list[dict[str, Any]] = []
        queue = list(root.get("tree", []))

        while queue:
            item = queue.pop()
            if item.get("type") == "blob":
                entries.append(item)
                continue
            if item.get("type") == "tree":
                subtree = self.client.get_tree(
                    owner,
                    name,
                    item["sha"],
                    recursive=False,
                )
                queue.extend(subtree.get("tree", []))

        return {
            "sha": root.get("sha"),
            "tree": entries,
            "truncated": False,
        }

    def _split(self, repository: str) -> str:
        ratio = self.config.output.validation_ratio
        if ratio <= 0:
            return "train"

        digest = hashlib.sha256(
            f"{self.config.output.split_seed}:{repository}".encode("utf-8")
        ).hexdigest()
        bucket = int(digest[:8], 16) / 0xFFFFFFFF
        return "validation" if bucket < ratio else "train"

    def _metadata_record(
        self,
        repo: dict[str, Any],
        file_info: dict[str, Any],
        tree: dict[str, Any],
        processed,
        split: str,
        source_url: str,
    ) -> dict[str, Any]:
        return {
            "repository": repo["full_name"],
            "file_path": file_info["path"],
            "branch": repo["default_branch"],
            "tree_sha": tree.get("sha"),
            "file_sha": file_info.get("sha"),
            "license": (repo.get("license") or {}).get("spdx_id"),
            "stars": repo.get("stargazers_count", 0),
            "source_url": source_url,
            "exact_hash": processed.exact_hash,
            "structural_hash": processed.structural_hash,
            "characters": len(processed.code),
            "non_empty_lines": processed.non_empty_lines,
            "tokens": processed.token_count,
            "split": split,
        }

    def _discover_candidates(self, state: CrawlState) -> dict[str, dict[str, Any]]:
        total_results = 0

        for query in self.config.github.queries:
            next_page, exhausted = state.get_query_progress(query.name, query.query)
            if exhausted:
                self.logger.info("Query '%s' is exhausted; skipping further discovery", query.name)
                continue

            self.logger.info(
                "Discovering query '%s' from page %d: %s",
                query.name,
                next_page,
                query.query,
            )
            results, new_next_page, query_exhausted = self.client.search_repositories(
                query.query,
                self.config.github.max_repositories,
                start_page=next_page,
                max_pages=self.config.github.max_pages_per_query,
            )
            total_results += len(results)

            for repo in results:
                name = repo_identity(repo)
                if not name:
                    continue

                accepted, reason = repository_passes_filters(
                    repo,
                    self.config.repository_filter,
                )
                if not accepted:
                    self.stats["repositories_rejected"] += 1
                    self.logger.debug(
                        "Repository rejected: %s (%s)",
                        name,
                        reason,
                    )
                    continue

                self.stats["repositories_accepted"] += 1
                state.upsert_candidate(
                    name,
                    int(repo.get("stargazers_count", 0)),
                    (repo.get("license") or {}).get("spdx_id"),
                    repo=repo,
                    query_name=query.name,
                )

            state.set_query_progress(
                query.name,
                query.query,
                new_next_page,
                query_exhausted,
            )

        candidates = state.get_unfinished_candidates()
        self.stats["queries"] = len(self.config.github.queries)
        self.stats["candidate_results"] = total_results
        self.stats["unique_candidates"] = len(candidates)
        self.stats["repositories_found"] = total_results
        return candidates

    def _record_row(self, processed, metadata: dict[str, Any]) -> dict[str, Any]:
        row = {"text": processed.code}
        if self.config.output.include_metadata_in_dataset:
            row["metadata"] = metadata
        return row

    def _process_file(
        self,
        state: CrawlState,
        repo: dict[str, Any],
        tree: dict[str, Any],
        file_info: dict[str, Any],
    ) -> str:
        repository = repo_identity(repo)
        file_path = file_info["path"]
        file_size = int(file_info.get("size", 0) or 0)

        if file_size > self.config.file_filter.max_file_size_bytes:
            self.stats["skipped_large_file"] += 1
            self.logger.debug(
                "Skipped large file %s:%s (%d bytes)",
                repository,
                file_path,
                file_size,
            )
            return "skipped_large_file"

        raw = self.client.get_blob(
            repo["owner"]["login"],
            repo["name"],
            file_info["sha"],
        )
        code = raw.decode("utf-8", errors="replace")

        try:
            processed = process_code(
                code,
                self.config.file_filter,
                self.config.quality,
            )
        except RejectedCode as exc:
            if exc.reason.startswith(QUALITY_REASONS):
                self.stats["rejected_quality"] += 1
            else:
                self.stats["rejected_cleaning"] += 1
            self.logger.debug(
                "Rejected %s:%s (%s)",
                repository,
                file_path,
                exc.reason,
            )
            return "rejected_quality"

        if self.config.security.enabled:
            findings = scan_for_secrets(processed.code)
            if findings:
                self.stats["rejected_security"] += 1
                types = sorted({finding["type"] for finding in findings})
                self.logger.warning(
                    "Security findings in %s:%s (%d finding(s): %s)",
                    repository,
                    file_path,
                    len(findings),
                    ", ".join(types),
                )
                if self.config.security.reject_on_detection:
                    return "rejected_security"

        if self.config.deduplication.exact and state.has_code_hash(
            "exact", processed.exact_hash
        ):
            self.stats["rejected_exact_duplicate"] += 1
            self.logger.debug(
                "Exact duplicate skipped: %s:%s",
                repository,
                file_path,
            )
            return "rejected_exact_duplicate"

        if self.config.deduplication.structural and state.has_code_hash(
            "structural", processed.structural_hash
        ):
            self.stats["rejected_structural_duplicate"] += 1
            self.logger.debug(
                "Structural duplicate skipped: %s:%s",
                repository,
                file_path,
            )
            return "rejected_structural_duplicate"

        split = self._split(repository)
        owner = repo["owner"]["login"]
        name = repo["name"]
        branch = repo["default_branch"]
        source_url = (
            f"https://github.com/{owner}/{name}"
            f"/blob/{branch}/{file_path}"
        )
        metadata = self._metadata_record(
            repo,
            file_info,
            tree,
            processed,
            split,
            source_url,
        )

        output = (
            self.config.output.validation_file
            if split == "validation"
            else self.config.output.train_file
        )
        dataset_record = self._record_row(processed, metadata)

        # ⭐ Total dataset storage guard. This counts the training + validation
        # JSONL files and excludes the manifest/provenance file.
        max_dataset_size_bytes = self.config.output.max_dataset_size_bytes
        current_bytes = total_dataset_size_bytes(
            self.config.output.train_file,
            self.config.output.validation_file,
        )
        incoming_bytes = jsonl_record_size_bytes(dataset_record)
        limit_bytes = max_dataset_size_bytes

        if limit_bytes > 0 and current_bytes + incoming_bytes > limit_bytes:
            self.logger.info(
                "Reached max_dataset_size_bytes=%d "
                "(current=%d bytes, next=%d bytes); stopping",
                limit_bytes,
                current_bytes,
                incoming_bytes,
            )
            return "storage_limit"

        append_jsonl(output, dataset_record)
        append_jsonl(self.config.output.metadata_file, metadata)

        state.remember_code_hash(
            "exact",
            processed.exact_hash,
            repository,
            file_path,
            split,
        )
        if self.config.deduplication.structural:
            state.remember_code_hash(
                "structural",
                processed.structural_hash,
                repository,
                file_path,
                split,
            )

        self.stats["files_accepted"] += 1
        return "accepted"

    def run(self) -> dict[str, Any]:
        self.config.runtime.work_dir.mkdir(parents=True, exist_ok=True)

        state = CrawlState(self.config.output.state_file)
        try:
            recovered = state.recover_in_progress()
            if recovered:
                self.logger.info("Recovered %d interrupted repositories", recovered)

            if self.config.output.mode == "overwrite":
                reset_outputs(
                    self.config.output.train_file,
                    self.config.output.validation_file,
                    self.config.output.metadata_file,
                )
                state.reset()
                self.logger.info("Output mode=overwrite; dataset and crawl state reset")

            current_dataset_bytes = total_dataset_size_bytes(
                self.config.output.train_file,
                self.config.output.validation_file,
            )
            self.stats["dataset_size_bytes"] = current_dataset_bytes
            max_dataset_size_bytes = self.config.output.max_dataset_size_bytes
            if max_dataset_size_bytes > 0 and current_dataset_bytes >= max_dataset_size_bytes:
                self.stats["storage_limit_reached"] = True
                self.logger.info(
                    "Dataset storage limit already reached: %d / %d bytes; stopping",
                    current_dataset_bytes,
                    max_dataset_size_bytes,
                )
                return self.stats

            candidates = self._discover_candidates(state)
            self.logger.info(
                "Candidate pool: %d unique repositories after filters",
                len(candidates),
            )

            selected, skipped = sample_repositories(
                candidates,
                self.config.github.queries,
                self.config.sampling,
                state,
                self.logger,
            )
            self.stats["repositories_selected"] = len(selected)
            self.stats["repositories_skipped_previously_crawled"] = skipped

            for item in selected:
                repo = item["repo"]
                repository = repo_identity(repo)
                state.mark_selected(repository)
                self.logger.info(
                    "Selected repository %s (stars=%d, queries=%s)",
                    repository,
                    int(repo.get("stargazers_count", 0)),
                    ",".join(sorted(item["query_names"])),
                )

                try:
                    tree = self._get_tree_entries(repo)
                    files = self._eligible_files(tree)
                    self.stats["files_found"] += len(files)
                    self.logger.info(
                        "Found %d eligible files in %s",
                        len(files),
                        repository,
                    )

                    limit = self.config.runtime.max_files_per_repository
                    if limit > 0:
                        files = files[:limit]

                    for file_info in files:
                        if (
                            self.config.runtime.max_output_records > 0
                            and self.stats["files_accepted"]
                            >= self.config.runtime.max_output_records
                        ):
                            break

                        self.stats["files_attempted"] += 1
                        try:
                            result = self._process_file(
                                state,
                                repo,
                                tree,
                                file_info,
                            )
                            if result == "storage_limit":
                                self.stats["storage_limit_reached"] = True
                                break
                            self.logger.debug(
                                "File result %s:%s -> %s",
                                repository,
                                file_info["path"],
                                result,
                            )
                        except GitHubAPIError as exc:
                            self.stats["errors"] += 1
                            self.logger.error(
                                "GitHub error for %s:%s: %s",
                                repository,
                                file_info["path"],
                                exc,
                            )
                            if not self.config.runtime.continue_on_repository_error:
                                raise
                            # A file-level API error should not mark the entire repo as
                            # permanently crawled; raise to the repository handler.
                            raise
                        except Exception as exc:
                            self.stats["errors"] += 1
                            self.logger.exception(
                                "Unexpected error for %s:%s: %s",
                                repository,
                                file_info["path"],
                                exc,
                            )
                            if not self.config.runtime.continue_on_repository_error:
                                raise
                            raise

                    if self.stats.get("storage_limit_reached"):
                        self.logger.info(
                            "Stopping crawl because the dataset storage limit was reached"
                        )
                        break

                    state.mark_crawled(repository)
                    self.stats["repositories_crawled"] += 1

                except Exception as exc:
                    self.stats["errors"] += 1
                    self.stats["repositories_failed"] += 1
                    state.mark_failed(repository, str(exc))
                    self.logger.exception(
                        "Repository failed: %s: %s",
                        repository,
                        exc,
                    )
                    if not self.config.runtime.continue_on_repository_error:
                        raise

                if self.stats.get("storage_limit_reached"):
                    break

                if (
                    self.config.runtime.max_output_records > 0
                    and self.stats["files_accepted"]
                    >= self.config.runtime.max_output_records
                ):
                    self.logger.info(
                        "Reached max_output_records=%d",
                        self.config.runtime.max_output_records,
                    )
                    break

            state_counts = state.counts()
            self.stats["state"] = state_counts
            (self.config.runtime.work_dir / "stats.json").write_text(
                json.dumps(self.stats, indent=2),
                encoding="utf-8",
            )
            self.stats["dataset_size_bytes"] = total_dataset_size_bytes(
                self.config.output.train_file,
                self.config.output.validation_file,
            )
            self.logger.info(
                "Crawl complete: %s",
                json.dumps(self.stats, sort_keys=True),
            )
            return self.stats
        finally:
            state.close()
