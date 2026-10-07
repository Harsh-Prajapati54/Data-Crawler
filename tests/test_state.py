from pathlib import Path

from github_code_crawler.state import CrawlState


def test_state_persists_repo_and_hashes(tmp_path: Path):
    db = tmp_path / "state.sqlite3"
    state = CrawlState(db)
    state.upsert_candidate("owner/repo", 123, "MIT")
    state.mark_selected("owner/repo")
    state.mark_crawled("owner/repo")
    state.remember_code_hash("exact", "abc", "owner/repo", "a.py", "train")
    state.commit()
    state.close()

    state = CrawlState(db)
    try:
        assert state.is_repository_crawled("owner/repo")
        assert state.has_code_hash("exact", "abc")
        assert not state.has_code_hash("exact", "def")
    finally:
        state.close()


def test_recover_processing_repos(tmp_path: Path):
    state = CrawlState(tmp_path / "state.sqlite3")
    state.upsert_candidate("owner/repo", 123, "MIT")
    state.mark_selected("owner/repo")
    assert state.recover_in_progress() == 1
    assert not state.is_repository_crawled("owner/repo")
    state.close()
