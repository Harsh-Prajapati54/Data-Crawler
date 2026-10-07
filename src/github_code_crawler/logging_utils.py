from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(log_level: str, work_dir: Path) -> logging.Logger:
    work_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("github_code_crawler")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(work_dir / "crawler.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
