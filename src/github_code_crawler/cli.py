from __future__ import annotations

import argparse
import sys

from .config import get_github_token, load_config
from .github_client import GitHubClient
from .logging_utils import setup_logging
from .pipeline import CrawlerPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a clean, diverse, deduplicated Python training dataset from GitHub."
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and initialize the GitHub client without crawling.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Search and display the repositories that would be selected, without downloading code.",
    )
    return parser


def _preview(config, logger, client) -> int:
    from .repository import repo_identity, repository_passes_filters
    from .sampler import sample_repositories
    from .state import CrawlState

    state = CrawlState(config.output.state_file)
    try:
        if config.output.mode == "overwrite":
            # Preview never resets files/state; it only demonstrates selection.
            logger.info("Preview mode: no output/state reset will be performed")

        candidates = state.get_unfinished_candidates()
        rejected = 0
        total = 0
        for query in config.github.queries:
            logger.info("Preview search '%s': %s", query.name, query.query)
            next_page, exhausted = state.get_query_progress(query.name, query.query)
            if exhausted:
                continue
            repos, new_next_page, query_exhausted = client.search_repositories(
                query.query,
                config.github.max_repositories,
                start_page=next_page,
                max_pages=config.github.max_pages_per_query,
            )
            total += len(repos)
            for repo in repos:
                name = repo_identity(repo)
                if not name:
                    continue
                accepted, _ = repository_passes_filters(repo, config.repository_filter)
                if not accepted:
                    rejected += 1
                    continue
                candidates.setdefault(name, {"repo": repo, "query_names": set()})
                candidates[name]["query_names"].add(query.name)

        selected, skipped = sample_repositories(
            candidates,
            config.github.queries,
            config.sampling,
            state,
            logger,
        )

        print("\nSelected repositories:\n")
        for index, item in enumerate(selected, 1):
            repo = item["repo"]
            print(
                f"{index:>3}. {repo['full_name']} "
                f"| stars={repo.get('stargazers_count', 0)} "
                f"| license={(repo.get('license') or {}).get('spdx_id')} "
                f"| queries={','.join(sorted(item['query_names']))}"
            )
        print(
            f"\nCandidates returned: {total}"
            f"\nUnique filtered candidates: {len(candidates)}"
            f"\nRejected by repository filter: {rejected}"
            f"\nPreviously crawled skipped: {skipped}"
            f"\nSelected this run: {len(selected)}"
        )
        return 0
    finally:
        state.close()


def main() -> int:
    args = build_parser().parse_args()

    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logger = setup_logging(
        config.runtime.log_level,
        config.runtime.work_dir,
    )
    token = get_github_token(config)

    if not token:
        logger.warning(
            "No %s token found. Public requests can still work, but authenticated rate limits are higher.",
            config.github.token_env,
        )

    client = GitHubClient(config.github, token, logger)
    try:
        if args.dry_run:
            logger.info(
                "Configuration OK. GitHub client initialized. Dry run complete."
            )
            return 0

        if args.preview:
            return _preview(config, logger, client)

        stats = CrawlerPipeline(config, logger, client).run()
        print("\nDataset build complete")
        print(f"Train:      {config.output.train_file}")
        print(f"Validation: {config.output.validation_file}")
        print(f"Manifest:   {config.output.metadata_file}")
        print(f"Accepted:   {stats['files_accepted']}")
        print(f"Repositories crawled: {stats['repositories_crawled']}")
        print(f"Repositories failed:  {stats['repositories_failed']}")
        return 0
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Crawler failed: %s", exc)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
