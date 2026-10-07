from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CrawlState:
    """Persistent state for resumable discovery, sampling, and deduplication."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS repositories (
                full_name TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                stars INTEGER NOT NULL DEFAULT 0,
                license TEXT,
                candidate_json TEXT NOT NULL,
                selected_at TEXT,
                crawled_at TEXT,
                updated_at TEXT NOT NULL,
                last_error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_repositories_status
                ON repositories(status);

            CREATE TABLE IF NOT EXISTS query_hits (
                full_name TEXT NOT NULL,
                query_name TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY(full_name, query_name),
                FOREIGN KEY(full_name) REFERENCES repositories(full_name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS query_progress (
                query_name TEXT PRIMARY KEY,
                query_text TEXT NOT NULL,
                next_page INTEGER NOT NULL DEFAULT 1,
                exhausted INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS code_hashes (
                hash_type TEXT NOT NULL,
                hash_value TEXT NOT NULL,
                repository TEXT NOT NULL,
                file_path TEXT NOT NULL,
                split TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(hash_type, hash_value)
            );

            CREATE INDEX IF NOT EXISTS idx_code_hashes_repository
                ON code_hashes(repository);
            """
        )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.executescript(
            """
            DELETE FROM code_hashes;
            DELETE FROM query_hits;
            DELETE FROM query_progress;
            DELETE FROM repositories;
            """
        )
        self.conn.commit()

    def recover_in_progress(self) -> int:
        now = utc_now()
        cursor = self.conn.execute(
            "UPDATE repositories SET status='new', updated_at=? WHERE status='processing'",
            (now,),
        )
        self.conn.commit()
        return cursor.rowcount

    def is_repository_crawled(self, full_name: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM repositories WHERE full_name=?",
            (full_name,),
        ).fetchone()
        return bool(row and row[0] == "crawled")

    def upsert_candidate(
        self,
        full_name: str,
        stars: int,
        license_id: str | None,
        repo: dict[str, Any] | None = None,
        query_name: str | None = None,
    ) -> None:
        now = utc_now()
        if repo is None:
            repo = {"full_name": full_name, "stargazers_count": stars}
        self.conn.execute(
            """
            INSERT INTO repositories(
                full_name, status, stars, license, candidate_json, updated_at
            ) VALUES (?, 'new', ?, ?, ?, ?)
            ON CONFLICT(full_name) DO UPDATE SET
                stars=excluded.stars,
                license=excluded.license,
                candidate_json=excluded.candidate_json,
                updated_at=excluded.updated_at
            """,
            (
                full_name,
                stars,
                license_id,
                json.dumps(repo, ensure_ascii=False),
                now,
            ),
        )
        if query_name:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO query_hits(full_name, query_name, first_seen_at)
                VALUES (?, ?, ?)
                """,
                (full_name, query_name, now),
            )
        self.conn.commit()

    def mark_selected(self, full_name: str) -> None:
        now = utc_now()
        self.conn.execute(
            """
            UPDATE repositories
            SET status='processing', selected_at=?, updated_at=?, last_error=NULL
            WHERE full_name=?
            """,
            (now, now, full_name),
        )
        self.conn.commit()

    def mark_crawled(self, full_name: str) -> None:
        now = utc_now()
        self.conn.execute(
            """
            UPDATE repositories
            SET status='crawled', crawled_at=?, updated_at=?, last_error=NULL
            WHERE full_name=?
            """,
            (now, now, full_name),
        )
        self.conn.commit()

    def mark_failed(self, full_name: str, error: str) -> None:
        now = utc_now()
        self.conn.execute(
            """
            UPDATE repositories
            SET status='failed', updated_at=?, last_error=?
            WHERE full_name=?
            """,
            (now, error[:2000], full_name),
        )
        self.conn.commit()

    def get_query_progress(self, query_name: str, query_text: str) -> tuple[int, bool]:
        row = self.conn.execute(
            "SELECT query_text, next_page, exhausted FROM query_progress WHERE query_name=?",
            (query_name,),
        ).fetchone()
        if row is None or row[0] != query_text:
            return 1, False
        return int(row[1]), bool(row[2])

    def set_query_progress(
        self,
        query_name: str,
        query_text: str,
        next_page: int,
        exhausted: bool,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO query_progress(query_name, query_text, next_page, exhausted, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(query_name) DO UPDATE SET
                query_text=excluded.query_text,
                next_page=excluded.next_page,
                exhausted=excluded.exhausted,
                updated_at=excluded.updated_at
            """,
            (
                query_name,
                query_text,
                next_page,
                int(exhausted),
                utc_now(),
            ),
        )
        self.conn.commit()

    def get_unfinished_candidates(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
                r.full_name,
                r.candidate_json,
                r.status,
                COALESCE(GROUP_CONCAT(q.query_name), '') AS query_names
            FROM repositories r
            LEFT JOIN query_hits q ON q.full_name = r.full_name
            WHERE r.status IN ('new', 'failed')
            GROUP BY r.full_name, r.candidate_json, r.status
            """
        ).fetchall()

        candidates: dict[str, dict[str, Any]] = {}
        for full_name, candidate_json, status, query_names in rows:
            try:
                repo = json.loads(candidate_json)
            except json.JSONDecodeError:
                continue
            candidates[full_name] = {
                "repo": repo,
                "query_names": {
                    name for name in str(query_names).split(",") if name
                },
                "status": status,
            }
        return candidates

    def has_code_hash(self, hash_type: str, hash_value: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM code_hashes WHERE hash_type=? AND hash_value=? LIMIT 1",
            (hash_type, hash_value),
        ).fetchone()
        return row is not None

    def remember_code_hash(
        self,
        hash_type: str,
        hash_value: str,
        repository: str,
        file_path: str,
        split: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO code_hashes(
                hash_type, hash_value, repository, file_path, split, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                hash_type,
                hash_value,
                repository,
                file_path,
                split,
                utc_now(),
            ),
        )
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM repositories GROUP BY status"
        ).fetchall()
        result: dict[str, int] = {status: int(count) for status, count in rows}
        result["query_hits"] = int(
            self.conn.execute("SELECT COUNT(*) FROM query_hits").fetchone()[0]
        )
        result["exact_hashes"] = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM code_hashes WHERE hash_type='exact'"
            ).fetchone()[0]
        )
        result["structural_hashes"] = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM code_hashes WHERE hash_type='structural'"
            ).fetchone()[0]
        )
        return result

    def close(self) -> None:
        self.conn.close()
