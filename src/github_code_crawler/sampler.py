from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

from .config import SamplingConfig, SearchQueryConfig, StarBucketConfig
from .repository import repo_identity
from .state import CrawlState


def _in_bucket(stars: int, bucket: StarBucketConfig) -> bool:
    return stars >= bucket.min_stars and (
        bucket.max_stars is None or stars <= bucket.max_stars
    )


def _allocate_counts(
    bucket_specs: list[tuple[str, float]],
    available: dict[str, int],
    total: int,
) -> dict[str, int]:
    allocations = {name: 0 for name, _ in bucket_specs}
    if total <= 0:
        return allocations

    weights = {name: weight for name, weight in bucket_specs}
    positive = [name for name, weight in bucket_specs if weight > 0 and available.get(name, 0) > 0]
    if not positive:
        return allocations

    raw = {name: total * weights[name] for name in positive}
    for name in positive:
        allocations[name] = min(available[name], int(raw[name]))

    assigned = sum(allocations.values())
    while assigned < total:
        candidates = [
            name
            for name in positive
            if allocations[name] < available[name]
        ]
        if not candidates:
            break
        candidates.sort(
            key=lambda name: (raw[name] - allocations[name], available[name] - allocations[name]),
            reverse=True,
        )
        allocations[candidates[0]] += 1
        assigned += 1
    return allocations


def _sample_one_group(
    candidates: list[dict[str, Any]],
    target: int,
    config: SamplingConfig,
    rng: random.Random,
) -> list[dict[str, Any]]:
    if target <= 0 or not candidates:
        return []

    target = min(target, len(candidates))

    if config.strategy == "top":
        return sorted(
            candidates,
            key=lambda item: int(item["repo"].get("stargazers_count", 0)),
            reverse=True,
        )[:target]

    if config.strategy == "random":
        return rng.sample(candidates, target)

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    remainder: list[dict[str, Any]] = []

    for item in candidates:
        stars = int(item["repo"].get("stargazers_count", 0))
        matched = False
        for bucket in config.star_buckets:
            if _in_bucket(stars, bucket):
                buckets[bucket.name].append(item)
                matched = True
                break
        if not matched:
            remainder.append(item)

    available = {name: len(items) for name, items in buckets.items()}
    allocations = _allocate_counts(
        [(b.name, b.weight) for b in config.star_buckets],
        available,
        target,
    )

    selected: list[dict[str, Any]] = []
    for bucket in config.star_buckets:
        pool = buckets[bucket.name][:]
        rng.shuffle(pool)
        selected.extend(pool[: allocations[bucket.name]])

    if len(selected) < target:
        remaining = [item for item in candidates if item not in selected]
        rng.shuffle(remaining)
        selected.extend(remaining[: target - len(selected)])

    rng.shuffle(selected)
    return selected[:target]


def _allocate_query_targets(
    query_configs: tuple[SearchQueryConfig, ...],
    availability: dict[str, int],
    total: int,
) -> dict[str, int]:
    positive = [q for q in query_configs if q.weight > 0 and availability.get(q.name, 0) > 0]
    targets = {q.name: 0 for q in query_configs}
    if not positive or total <= 0:
        return targets

    weight_sum = sum(q.weight for q in positive)
    raw = {q.name: total * q.weight / weight_sum for q in positive}

    for q in positive:
        targets[q.name] = min(availability[q.name], int(raw[q.name]))

    assigned = sum(targets.values())
    while assigned < total:
        choices = [
            q for q in positive
            if targets[q.name] < availability[q.name]
        ]
        if not choices:
            break
        choices.sort(
            key=lambda q: (raw[q.name] - targets[q.name], availability[q.name] - targets[q.name]),
            reverse=True,
        )
        targets[choices[0].name] += 1
        assigned += 1

    return targets


def sample_repositories(
    candidates: dict[str, dict[str, Any]],
    query_configs: tuple[SearchQueryConfig, ...],
    config: SamplingConfig,
    state: CrawlState,
    logger,
) -> tuple[list[dict[str, Any]], int]:
    """Sample unseen repositories across query groups and star bands."""
    unseen: dict[str, dict[str, Any]] = {}
    skipped_crawled = 0

    for full_name, item in candidates.items():
        if config.avoid_previously_crawled and state.is_repository_crawled(full_name):
            skipped_crawled += 1
            continue
        unseen[full_name] = item

    target = min(config.repositories_per_run, len(unseen))
    if target <= 0:
        return [], skipped_crawled

    rng = random.Random(config.seed)
    candidate_values = list(unseen.values())
    if config.candidate_pool_size > 0 and len(candidate_values) > config.candidate_pool_size:
        candidate_values = rng.sample(candidate_values, config.candidate_pool_size)

    by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidate_values:
        for query_name in item["query_names"]:
            by_query[query_name].append(item)

    availability = {query.name: len(by_query.get(query.name, [])) for query in query_configs}
    targets = _allocate_query_targets(query_configs, availability, target)

    selected: list[dict[str, Any]] = []
    selected_names: set[str] = set()

    # Query-level quotas provide topical/search diversity. Star buckets provide popularity diversity.
    for query in query_configs:
        pool = [item for item in by_query.get(query.name, []) if repo_identity(item["repo"]) not in selected_names]
        group_selected = _sample_one_group(pool, targets[query.name], config, rng)
        for item in group_selected:
            name = repo_identity(item["repo"])
            if name not in selected_names:
                selected.append(item)
                selected_names.add(name)

    # Some repositories may belong to several query groups, so fill any unassigned slots globally.
    if len(selected) < target:
        remaining = [item for item in unseen.values() if repo_identity(item["repo"]) not in selected_names]
        selected.extend(_sample_one_group(remaining, target - len(selected), config, rng))

    logger.info(
        "Sampling strategy=%s candidate_pool=%d unseen=%d selected=%d skipped_previously_crawled=%d",
        config.strategy,
        len(candidate_values),
        len(unseen),
        len(selected),
        skipped_crawled,
    )
    return selected[:target], skipped_crawled
