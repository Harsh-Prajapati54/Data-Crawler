from __future__ import annotations

import ast
import io
import keyword
import re
import tokenize
from dataclasses import dataclass
from hashlib import sha256

from .config import FileFilterConfig, QualityConfig


@dataclass(frozen=True)
class ProcessedCode:
    code: str
    exact_hash: str
    structural_hash: str
    non_empty_lines: int
    token_count: int


class RejectedCode(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def clean_code(code: str, file_config: FileFilterConfig, quality_config: QualityConfig) -> str:
    if not code:
        raise RejectedCode("empty_file")

    code = code.lstrip("\ufeff")
    if quality_config.reject_binary and "\x00" in code:
        raise RejectedCode("binary_content")

    code = code.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in code.split("\n")]
    code = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    if len(code) < file_config.min_code_characters:
        raise RejectedCode("too_few_characters")
    if len(code) > file_config.max_code_characters:
        raise RejectedCode("too_many_characters")

    non_empty_lines = [line for line in code.splitlines() if line.strip()]
    if len(non_empty_lines) < file_config.min_code_lines:
        raise RejectedCode("too_few_code_lines")

    return code


def _looks_generated(code: str, markers: tuple[str, ...]) -> bool:
    sample = "\n".join(code.splitlines()[:80]).lower()
    return any(marker in sample for marker in markers)


def _repeated_line_ratio(code: str) -> float:
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    if not lines:
        return 0.0
    unique = len(set(lines))
    return 1.0 - (unique / len(lines))


def _tokenize(code: str) -> list[tokenize.TokenInfo]:
    tokens = []
    reader = io.StringIO(code).readline
    for token in tokenize.generate_tokens(reader):
        if token.type in {
            tokenize.ENCODING,
            tokenize.ENDMARKER,
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.COMMENT,
        }:
            continue
        tokens.append(token)
    return tokens


def validate_python(code: str) -> None:
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise RejectedCode(f"syntax_error:{exc.msg}") from exc


def structural_fingerprint(code: str) -> str:
    """Create a conservative structural fingerprint.

    Names, strings and numeric literals are normalized, while Python
    keywords/operators remain. This catches simple renamed-variable copies
    without requiring an expensive all-pairs similarity search.
    """
    out: list[str] = []
    for token in _tokenize(code):
        if token.type == tokenize.NAME:
            out.append(token.string if keyword.iskeyword(token.string) else "NAME")
        elif token.type == tokenize.STRING:
            out.append("STRING")
        elif token.type == tokenize.NUMBER:
            out.append("NUMBER")
        else:
            out.append(token.string)
    normalized = " ".join(out)
    return sha256(normalized.encode("utf-8")).hexdigest()


def process_code(code: str, file_config: FileFilterConfig, quality_config: QualityConfig) -> ProcessedCode:
    cleaned = clean_code(code, file_config, quality_config)

    if quality_config.reject_generated_code and _looks_generated(cleaned, quality_config.generated_markers):
        raise RejectedCode("generated_code")

    if _repeated_line_ratio(cleaned) > quality_config.max_repeated_line_ratio:
        raise RejectedCode("high_line_repetition")

    if quality_config.syntax_check:
        validate_python(cleaned)

    try:
        token_count = len(_tokenize(cleaned))
    except (tokenize.TokenError, IndentationError) as exc:
        raise RejectedCode(f"tokenization_error:{exc}") from exc

    if token_count < file_config.min_tokens:
        raise RejectedCode("too_few_tokens")

    exact_hash = sha256(cleaned.encode("utf-8")).hexdigest()
    structural_hash = structural_fingerprint(cleaned)

    return ProcessedCode(
        code=cleaned,
        exact_hash=exact_hash,
        structural_hash=structural_hash,
        non_empty_lines=sum(1 for line in cleaned.splitlines() if line.strip()),
        token_count=token_count,
    )
