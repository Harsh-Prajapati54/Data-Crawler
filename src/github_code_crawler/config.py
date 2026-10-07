from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class SearchQueryConfig:
    name: str
    query: str
    weight: float


@dataclass(frozen=True)
class GitHubConfig:
    token_env: str
    api_base_url: str
    api_version: str
    queries: tuple[SearchQueryConfig, ...]
    sort: str
    order: str
    max_repositories: int
    page_size: int
    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    max_pages_per_query: int


@dataclass(frozen=True)
class RepositoryFilterConfig:
    min_stars: int
    max_size_bytes: int
    require_license: bool
    allowed_licenses: frozenset[str]
    exclude_archived: bool
    exclude_forks: bool


@dataclass(frozen=True)
class FileFilterConfig:
    extensions: tuple[str, ...]
    max_file_size_bytes: int
    min_code_characters: int
    min_code_lines: int
    min_tokens: int
    max_code_characters: int
    ignore_directories: frozenset[str]


@dataclass(frozen=True)
class QualityConfig:
    syntax_check: bool
    reject_generated_code: bool
    generated_markers: tuple[str, ...]
    reject_binary: bool
    max_repeated_line_ratio: float


@dataclass(frozen=True)
class SecurityConfig:
    enabled: bool
    reject_on_detection: bool


@dataclass(frozen=True)
class DedupConfig:
    exact: bool
    structural: bool


@dataclass(frozen=True)
class StarBucketConfig:
    name: str
    min_stars: int
    max_stars: int | None
    weight: float


@dataclass(frozen=True)
class SamplingConfig:
    strategy: str
    repositories_per_run: int
    candidate_pool_size: int
    seed: int
    avoid_previously_crawled: bool
    star_buckets: tuple[StarBucketConfig, ...]


@dataclass(frozen=True)
class OutputConfig:
    mode: str
    train_file: Path
    validation_file: Path
    metadata_file: Path
    validation_ratio: float
    split_seed: int
    include_metadata_in_dataset: bool
    state_file: Path
    max_dataset_size_bytes: int


@dataclass(frozen=True)
class RuntimeConfig:
    work_dir: Path
    log_level: str
    continue_on_repository_error: bool
    max_files_per_repository: int
    max_output_records: int


@dataclass(frozen=True)
class AppConfig:
    github: GitHubConfig
    repository_filter: RepositoryFilterConfig
    file_filter: FileFilterConfig
    quality: QualityConfig
    security: SecurityConfig
    deduplication: DedupConfig
    sampling: SamplingConfig
    output: OutputConfig
    runtime: RuntimeConfig


def _require(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ValueError(f"Missing configuration key: {key}")
    return mapping[key]


def _as_int(value: Any, key: str, minimum: int = 0) -> int:
    value = int(value)
    if value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def _parse_queries(gh: dict[str, Any]) -> tuple[SearchQueryConfig, ...]:
    raw_queries = gh.get("queries")
    if raw_queries is None:
        query = str(_require(gh, "query"))
        return (SearchQueryConfig(name="default", query=query, weight=1.0),)

    if not raw_queries:
        raise ValueError("github.queries cannot be empty")

    parsed: list[SearchQueryConfig] = []
    for index, item in enumerate(raw_queries):
        if isinstance(item, str):
            parsed.append(SearchQueryConfig(name=f"query_{index + 1}", query=item, weight=1.0))
            continue
        if not isinstance(item, dict):
            raise ValueError("Each github.queries item must be a string or mapping")
        weight = float(item.get("weight", 1.0))
        if weight < 0:
            raise ValueError(f"github.queries[{index}].weight must be >= 0")
        parsed.append(
            SearchQueryConfig(
                name=str(item.get("name", f"query_{index + 1}")),
                query=str(_require(item, "query")),
                weight=weight,
            )
        )
    if not any(query.weight > 0 for query in parsed):
        raise ValueError("At least one github query must have weight > 0")
    return tuple(parsed)


def _parse_star_buckets(raw: Any) -> tuple[StarBucketConfig, ...]:
    if raw is None:
        raw = [
            {"name": "very_high", "min_stars": 10000, "max_stars": None, "weight": 0.10},
            {"name": "high", "min_stars": 2000, "max_stars": 9999, "weight": 0.25},
            {"name": "medium", "min_stars": 500, "max_stars": 1999, "weight": 0.35},
            {"name": "low", "min_stars": 100, "max_stars": 499, "weight": 0.30},
        ]

    buckets: list[StarBucketConfig] = []
    total_weight = 0.0
    names: set[str] = set()
    for item in raw:
        name = str(_require(item, "name"))
        if name in names:
            raise ValueError(f"Duplicate sampling.star_buckets name: {name}")
        names.add(name)
        min_stars = _as_int(_require(item, "min_stars"), f"sampling.star_buckets[{name}].min_stars")
        max_raw = item.get("max_stars")
        max_stars = None if max_raw is None else _as_int(max_raw, f"sampling.star_buckets[{name}].max_stars")
        if max_stars is not None and max_stars < min_stars:
            raise ValueError(f"sampling.star_buckets[{name}] max_stars < min_stars")
        weight = float(_require(item, "weight"))
        if weight < 0:
            raise ValueError(f"sampling.star_buckets[{name}].weight must be >= 0")
        total_weight += weight
        buckets.append(StarBucketConfig(name, min_stars, max_stars, weight))

    if not buckets:
        raise ValueError("sampling.star_buckets cannot be empty")
    if total_weight <= 0:
        raise ValueError("sampling.star_buckets weights must sum to > 0")

    # Normalize instead of forcing the user to make weights sum to exactly 1.
    return tuple(
        StarBucketConfig(b.name, b.min_stars, b.max_stars, b.weight / total_weight)
        for b in buckets
    )


def load_config(path: str | Path) -> AppConfig:
    """Load and validate the crawler configuration."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    load_dotenv()

    gh = _require(raw, "github")
    rf = _require(raw, "repository_filter")
    ff = _require(raw, "file_filter")
    q = _require(raw, "quality")
    sec = _require(raw, "security")
    dd = _require(raw, "deduplication")
    sampling = _require(raw, "sampling")
    out = _require(raw, "output")
    rt = _require(raw, "runtime")

    extensions = tuple(str(x).lower() for x in _require(ff, "extensions"))
    if not extensions:
        raise ValueError("file_filter.extensions cannot be empty")

    allowed_licenses = frozenset(str(x) for x in _require(rf, "allowed_licenses"))
    if rf.get("require_license", True) and not allowed_licenses:
        raise ValueError("allowed_licenses cannot be empty when require_license=true")

    validation_ratio = float(_require(out, "validation_ratio"))
    if not 0 <= validation_ratio < 1:
        raise ValueError("output.validation_ratio must be in [0, 1)")

    repeated_ratio = float(_require(q, "max_repeated_line_ratio"))
    if not 0 <= repeated_ratio <= 1:
        raise ValueError("quality.max_repeated_line_ratio must be in [0, 1]")

    mode = str(out.get("mode", "append")).lower()
    if mode not in {"append", "overwrite"}:
        raise ValueError("output.mode must be append or overwrite")

    strategy = str(sampling.get("strategy", "stratified")).lower()
    if strategy not in {"top", "random", "stratified"}:
        raise ValueError("sampling.strategy must be top, random, or stratified")

    repositories_per_run = _as_int(
        sampling.get("repositories_per_run", 100),
        "sampling.repositories_per_run",
    )
    if repositories_per_run == 0:
        raise ValueError("sampling.repositories_per_run must be > 0")

    max_size_bytes = _as_int(
        _require(rf, "max_size_bytes"),
        "repository_filter.max_size_bytes",
        minimum=1,
    )

    max_file_size_bytes = _as_int(
        _require(ff, "max_file_size_bytes"),
        "file_filter.max_file_size_bytes",
        minimum=1,
    )

    max_dataset_size_bytes = _as_int(
        out.get("max_dataset_size_bytes", 0),
        "output.max_dataset_size_bytes",
        minimum=0,
    )

    return AppConfig(
        github=GitHubConfig(
            token_env=str(_require(gh, "token_env")),
            api_base_url=str(_require(gh, "api_base_url")).rstrip("/"),
            api_version=str(_require(gh, "api_version")),
            queries=_parse_queries(gh),
            sort=str(gh.get("sort", "stars")),
            order=str(gh.get("order", "desc")),
            max_repositories=_as_int(
                gh.get("max_repositories", 1000),
                "github.max_repositories",
            ),
            page_size=min(_as_int(gh.get("page_size", 100), "github.page_size", 1), 100),
            timeout_seconds=float(gh.get("timeout_seconds", 30)),
            max_retries=_as_int(gh.get("max_retries", 4), "github.max_retries"),
            retry_backoff_seconds=float(gh.get("retry_backoff_seconds", 2)),
            max_pages_per_query=_as_int(
                gh.get("max_pages_per_query", 10),
                "github.max_pages_per_query",
            ),
        ),
        repository_filter=RepositoryFilterConfig(
            min_stars=int(_require(rf, "min_stars")),
            max_size_bytes=max_size_bytes,
            require_license=bool(rf.get("require_license", True)),
            allowed_licenses=allowed_licenses,
            exclude_archived=bool(rf.get("exclude_archived", True)),
            exclude_forks=bool(rf.get("exclude_forks", True)),
        ),
        file_filter=FileFilterConfig(
            extensions=extensions,
            max_file_size_bytes=max_file_size_bytes,
            min_code_characters=int(_require(ff, "min_code_characters")),
            min_code_lines=int(_require(ff, "min_code_lines")),
            min_tokens=int(_require(ff, "min_tokens")),
            max_code_characters=int(_require(ff, "max_code_characters")),
            ignore_directories=frozenset(
                str(x).strip("/").lower() for x in _require(ff, "ignore_directories")
            ),
        ),
        quality=QualityConfig(
            syntax_check=bool(q.get("syntax_check", True)),
            reject_generated_code=bool(q.get("reject_generated_code", True)),
            generated_markers=tuple(str(x).lower() for x in _require(q, "generated_markers")),
            reject_binary=bool(q.get("reject_binary", True)),
            max_repeated_line_ratio=repeated_ratio,
        ),
        security=SecurityConfig(
            enabled=bool(sec.get("enabled", True)),
            reject_on_detection=bool(sec.get("reject_on_detection", True)),
        ),
        deduplication=DedupConfig(
            exact=bool(dd.get("exact", True)),
            structural=bool(dd.get("structural", False)),
        ),
        sampling=SamplingConfig(
            strategy=strategy,
            repositories_per_run=repositories_per_run,
            candidate_pool_size=_as_int(
                sampling.get("candidate_pool_size", 1000),
                "sampling.candidate_pool_size",
            ),
            seed=int(sampling.get("seed", 42)),
            avoid_previously_crawled=bool(sampling.get("avoid_previously_crawled", True)),
            star_buckets=_parse_star_buckets(sampling.get("star_buckets")),
        ),
        output=OutputConfig(
            mode=mode,
            train_file=Path(_require(out, "train_file")),
            validation_file=Path(_require(out, "validation_file")),
            metadata_file=Path(_require(out, "metadata_file")),
            validation_ratio=validation_ratio,
            split_seed=int(_require(out, "split_seed")),
            include_metadata_in_dataset=bool(out.get("include_metadata_in_dataset", False)),
            state_file=Path(out.get("state_file", "run/state.sqlite3")),
            max_dataset_size_bytes=max_dataset_size_bytes,
        ),
        runtime=RuntimeConfig(
            work_dir=Path(_require(rt, "work_dir")),
            log_level=str(rt.get("log_level", "INFO")).upper(),
            continue_on_repository_error=bool(rt.get("continue_on_repository_error", True)),
            max_files_per_repository=_as_int(
                rt.get("max_files_per_repository", 0),
                "runtime.max_files_per_repository",
            ),
            max_output_records=_as_int(
                rt.get("max_output_records", 0),
                "runtime.max_output_records",
            ),
        ),
    )


def get_github_token(config: AppConfig) -> str | None:
    return os.getenv(config.github.token_env)
