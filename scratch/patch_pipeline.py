import re
import os

with open(r'c:\Users\HarshPrajapati\Downloads\github-code-crawler-v0.2.0\github-code-crawler\src\github_code_crawler\pipeline.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update imports
content = content.replace(
    "from .dataset import (",
    "import os\nimport zipfile\nimport tarfile\nfrom .dataset import (\n    append_jsonl_batch,"
)

# 2. Refactor _process_file to _process_file_content
old_process_file = """    def _process_file(
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
        code = raw.decode("utf-8", errors="replace")"""

new_process_file = """    def _process_file_content(
        self,
        state: CrawlState,
        repo: dict[str, Any],
        file_path: str,
        file_size: int,
        code: str,
        commit_sha: str,
        batch_train: list,
        batch_val: list,
        batch_meta: list,
    ) -> str:
        repository = repo_identity(repo)

        if file_size > self.config.file_filter.max_file_size_bytes:
            self.stats["skipped_large_file"] += 1
            self.logger.debug(
                "Skipped large file %s:%s (%d bytes)",
                repository,
                file_path,
                file_size,
            )
            return "skipped_large_file"
"""

content = content.replace(old_process_file, new_process_file)

# 3. Update metadata record call in _process_file_content
content = content.replace(
    """        metadata = self._metadata_record(
            repo,
            file_info,
            tree,
            processed,
            split,
            source_url,
        )""",
    """        metadata = {
            "repository": repo["full_name"],
            "file_path": file_path,
            "branch": repo["default_branch"],
            "tree_sha": commit_sha,
            "file_sha": commit_sha, # Using commit_sha as a proxy for file_sha in archive mode
            "license": (repo.get("license") or {}).get("spdx_id"),
            "stars": repo.get("stargazers_count", 0),
            "source_url": source_url,
            "exact_hash": processed.exact_hash,
            "structural_hash": processed.structural_hash,
            "characters": len(processed.code),
            "non_empty_lines": processed.non_empty_lines,
            "tokens": processed.token_count,
            "split": split,
        }"""
)

# Remove the old _metadata_record method definition since we inline it or don't need it. We can just leave it for now.

# 4. Update dataset write in _process_file_content
old_dataset_write = """        # ⭐ Total dataset storage guard. This counts the training + validation
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
        append_jsonl(self.config.output.metadata_file, metadata)"""

new_dataset_write = """        max_dataset_size_bytes = self.config.output.max_dataset_size_bytes
        incoming_bytes = jsonl_record_size_bytes(dataset_record)
        
        if max_dataset_size_bytes > 0 and self.current_dataset_bytes + incoming_bytes > max_dataset_size_bytes:
            self.logger.info(
                "Reached max_dataset_size_bytes=%d "
                "(current=%d bytes, next=%d bytes); stopping",
                max_dataset_size_bytes,
                self.current_dataset_bytes,
                incoming_bytes,
            )
            return "storage_limit"

        self.current_dataset_bytes += incoming_bytes

        if split == "validation":
            batch_val.append(dataset_record)
        else:
            batch_train.append(dataset_record)
        batch_meta.append(metadata)"""

content = content.replace(old_dataset_write, new_dataset_write)

# 5. Fix run() logic for processing
old_run_repo_processing = """                try:
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
                    self.stats["repositories_crawled"] += 1"""


new_run_repo_processing = """                try:
                    batch_train = []
                    batch_val = []
                    batch_meta = []
                    
                    owner = repo["owner"]["login"]
                    name = repo["name"]
                    branch = repo["default_branch"]
                    
                    if self.config.github.download_method == "archive":
                        commit_sha = self.client.get_commit_sha(owner, name, branch)
                        archive_path = self.client.download_archive(owner, name, commit_sha, self.config.github.archive_format)
                        
                        try:
                            # Process archive locally
                            if self.config.github.archive_format == "zip":
                                with zipfile.ZipFile(archive_path, 'r') as z:
                                    entries = z.infolist()
                                    # Filter entries
                                    files = []
                                    for entry in entries:
                                        if entry.is_dir(): continue
                                        # Zip paths usually have a root folder like repo-commitSHA/
                                        parts = entry.filename.split('/', 1)
                                        if len(parts) < 2: continue
                                        file_path = parts[1]
                                        
                                        if self._ignored_path(file_path): continue
                                        if not any(file_path.lower().endswith(ext) for ext in self.config.file_filter.extensions): continue
                                        
                                        files.append((file_path, entry.file_size, entry))
                                    
                                    self.stats["files_found"] += len(files)
                                    limit = self.config.runtime.max_files_per_repository
                                    if limit > 0: files = files[:limit]
                                    
                                    for file_path, file_size, entry in files:
                                        if self.config.runtime.max_output_records > 0 and self.stats["files_accepted"] >= self.config.runtime.max_output_records: break
                                        self.stats["files_attempted"] += 1
                                        
                                        with z.open(entry) as f:
                                            code_bytes = f.read()
                                        code_str = code_bytes.decode("utf-8", errors="replace")
                                        
                                        result = self._process_file_content(state, repo, file_path, file_size, code_str, commit_sha, batch_train, batch_val, batch_meta)
                                        if result == "storage_limit":
                                            self.stats["storage_limit_reached"] = True
                                            break
                            else:
                                with tarfile.open(archive_path, 'r:*') as t:
                                    files = []
                                    for member in t.getmembers():
                                        if not member.isfile(): continue
                                        parts = member.name.split('/', 1)
                                        if len(parts) < 2: continue
                                        file_path = parts[1]
                                        
                                        if self._ignored_path(file_path): continue
                                        if not any(file_path.lower().endswith(ext) for ext in self.config.file_filter.extensions): continue
                                        
                                        files.append((file_path, member.size, member))
                                    
                                    self.stats["files_found"] += len(files)
                                    limit = self.config.runtime.max_files_per_repository
                                    if limit > 0: files = files[:limit]
                                    
                                    for file_path, file_size, member in files:
                                        if self.config.runtime.max_output_records > 0 and self.stats["files_accepted"] >= self.config.runtime.max_output_records: break
                                        self.stats["files_attempted"] += 1
                                        
                                        f = t.extractfile(member)
                                        if f is None: continue
                                        code_bytes = f.read()
                                        code_str = code_bytes.decode("utf-8", errors="replace")
                                        
                                        result = self._process_file_content(state, repo, file_path, file_size, code_str, commit_sha, batch_train, batch_val, batch_meta)
                                        if result == "storage_limit":
                                            self.stats["storage_limit_reached"] = True
                                            break
                        finally:
                            if os.path.exists(archive_path):
                                os.remove(archive_path)
                    
                    else:
                        # Blob fallback
                        tree = self._get_tree_entries(repo)
                        files = self._eligible_files(tree)
                        self.stats["files_found"] += len(files)
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
                                file_path = file_info["path"]
                                file_size = int(file_info.get("size", 0) or 0)
                                raw = self.client.get_blob(owner, name, file_info["sha"])
                                code_str = raw.decode("utf-8", errors="replace")
                                commit_sha = tree.get("sha", "")
                                
                                result = self._process_file_content(state, repo, file_path, file_size, code_str, commit_sha, batch_train, batch_val, batch_meta)
                                if result == "storage_limit":
                                    self.stats["storage_limit_reached"] = True
                                    break
                            except GitHubAPIError as exc:
                                self.stats["errors"] += 1
                                if not self.config.runtime.continue_on_repository_error:
                                    raise
                                raise

                    # Flush batches
                    if batch_train:
                        append_jsonl_batch(self.config.output.train_file, batch_train)
                    if batch_val:
                        append_jsonl_batch(self.config.output.validation_file, batch_val)
                    if batch_meta:
                        append_jsonl_batch(self.config.output.metadata_file, batch_meta)
                        
                    # Commit SQLite state for this repo
                    state.commit()

                    if self.stats.get("storage_limit_reached"):
                        self.logger.info(
                            "Stopping crawl because the dataset storage limit was reached"
                        )
                        break

                    state.mark_crawled(repository)
                    self.stats["repositories_crawled"] += 1"""

content = content.replace(old_run_repo_processing, new_run_repo_processing)

# 6. Add self.current_dataset_bytes initialization
content = content.replace(
    """            self.stats["dataset_size_bytes"] = current_dataset_bytes
            max_dataset_size_bytes = self.config.output.max_dataset_size_bytes""",
    """            self.stats["dataset_size_bytes"] = current_dataset_bytes
            self.current_dataset_bytes = current_dataset_bytes
            max_dataset_size_bytes = self.config.output.max_dataset_size_bytes"""
)

# Ensure batch_val and batch_meta imports exist in dataset, oh we just used append_jsonl_batch.

with open(r'c:\Users\HarshPrajapati\Downloads\github-code-crawler-v0.2.0\github-code-crawler\src\github_code_crawler\pipeline.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Pipeline modified successfully!")
