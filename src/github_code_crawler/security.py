from __future__ import annotations

import tempfile
from pathlib import Path

from detect_secrets import SecretsCollection
from detect_secrets.settings import default_settings


def scan_for_secrets(code: str) -> list[dict]:
    """Return safe finding metadata; never expose the secret value."""
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            encoding="utf-8",
            newline="",
        ) as handle:
            handle.write(code)
            temp_path = handle.name

        collection = SecretsCollection()
        with default_settings():
            collection.scan_file(temp_path)

        findings: list[dict] = []
        for _, items in collection.json().items():
            for finding in items:
                findings.append(
                    {
                        "type": finding.get("type", "Unknown"),
                        "line_number": finding.get("line_number"),
                    }
                )
        return findings
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)
