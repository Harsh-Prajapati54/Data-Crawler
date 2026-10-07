from pathlib import Path

from github_code_crawler.config import SamplingConfig, SearchQueryConfig, StarBucketConfig
from github_code_crawler.sampler import sample_repositories
from github_code_crawler.state import CrawlState


def make_repo(i: int, stars: int) -> dict:
    return {
        "full_name": f"owner/repo-{i}",
        "owner": {"login": "owner"},
        "name": f"repo-{i}",
        "stargazers_count": stars,
    }


def make_config() -> SamplingConfig:
    return SamplingConfig(
        strategy="stratified",
        repositories_per_run=4,
        candidate_pool_size=100,
        seed=42,
        avoid_previously_crawled=True,
        star_buckets=(
            StarBucketConfig("high", 1000, None, 0.5),
            StarBucketConfig("low", 100, 999, 0.5),
        ),
    )


def test_sampling_excludes_crawled_and_uses_query_mix(tmp_path: Path):
    state = CrawlState(tmp_path / "state.sqlite3")
    try:
        state.upsert_candidate("owner/repo-0", 5000, "MIT")
        state.mark_selected("owner/repo-0")
        state.mark_crawled("owner/repo-0")

        candidates = {}
        for i, stars in enumerate([5000, 4000, 3000, 900, 800, 700, 600]):
            repo = make_repo(i, stars)
            candidates[repo["full_name"]] = {
                "repo": repo,
                "query_names": {"general" if i % 2 == 0 else "ml"},
            }

        queries = (
            SearchQueryConfig("general", "language:python", 0.5),
            SearchQueryConfig("ml", "language:python topic:machine-learning", 0.5),
        )

        selected, skipped = sample_repositories(
            candidates,
            queries,
            make_config(),
            state,
            _NullLogger(),
        )

        assert skipped == 1
        assert len(selected) == 4
        assert all(item["repo"]["full_name"] != "owner/repo-0" for item in selected)
    finally:
        state.close()


class _NullLogger:
    def info(self, *args, **kwargs):
        pass
