from __future__ import annotations

import json
from pathlib import Path


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def reset_outputs(*paths: Path) -> None:
    for path in paths:
        ensure_parent(path)
        path.write_text("", encoding="utf-8")


def append_jsonl(path: Path, record: dict) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_jsonl_batch(path: Path, records: list[dict]) -> None:
    if not records:
        return
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def file_size_bytes(path: Path) -> int:
    """Return file size in bytes; missing files count as zero."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def total_dataset_size_bytes(*paths: Path) -> int:
    """Return combined size of dataset output files."""
    return sum(file_size_bytes(path) for path in paths)


def jsonl_record_size_bytes(record: dict) -> int:
    """Return the exact number of UTF-8 bytes this JSONL record will add."""
    line = json.dumps(record, ensure_ascii=False) + "\n"
    return len(line.encode("utf-8"))
