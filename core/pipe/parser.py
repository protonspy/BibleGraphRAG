"""Parser step — normalize raw biblical text into structured JSON (content + ref).

Project conventions: inputs live in data/*.txt, outputs in output/*.json.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")

# Records are separated by a line containing only a period.
RECORD_SEPARATOR = re.compile(r"(?m)^\s*\.\s*$")
# Delimiter between a verse's content and its reference.
REF_DELIMITER = " -- "
# Reference shape at the end of a record: "<book> <chapter>:<verse>".
REF_PATTERN = re.compile(r"^(?P<book>.+?)\s+(?P<chapter>\d+):(?P<verse>\d+)$")

# Connector words kept lowercase when normalizing a book name (unless first word).
_SMALL_WORDS = {"of", "the", "and", "a", "an", "to", "in", "on", "for"}


def normalize_book(raw: str) -> str:
    """Title-case a book name, keeping connector words lowercase and numeric prefixes intact.

    'genesis' -> 'Genesis', 'song of songs' -> 'Song of Songs', '1 samuel' -> '1 Samuel'.
    """
    words = raw.strip().split()
    out: list[str] = []
    for i, word in enumerate(words):
        lower = word.lower()
        if word[:1].isdigit():
            out.append(word)
        elif i > 0 and lower in _SMALL_WORDS:
            out.append(lower)
        else:
            out.append(lower.capitalize())
    return " ".join(out)


def normalize_content(raw: str) -> str:
    """Collapse internal whitespace/newlines into single spaces and trim."""
    return " ".join(raw.split())


def parse_text(text: str) -> tuple[list[dict], list[str]]:
    """Parse raw source text into structured verse records.

    Returns (records, warnings).
    """
    records: list[dict] = []
    warnings: list[str] = []

    for chunk in RECORD_SEPARATOR.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if REF_DELIMITER not in chunk:
            warnings.append(f"missing '{REF_DELIMITER.strip()}' delimiter: {chunk[:60]!r}")
            continue
        content_raw, ref_raw = chunk.rsplit(REF_DELIMITER, 1)
        ref_raw = ref_raw.strip()
        match = REF_PATTERN.match(ref_raw)
        if not match:
            warnings.append(f"unparseable ref: {ref_raw!r}")
            continue
        records.append(
            {
                "content": normalize_content(content_raw),
                "book": normalize_book(match.group("book")),
                "chapter": int(match.group("chapter")),
                "verse": int(match.group("verse")),
                "ref": ref_raw,
            }
        )
    return records, warnings


def _next_output_path(name: str) -> Path:
    """Return output/<name>-<N>.json with the smallest free N (never overwrites)."""
    n = 1
    while (OUTPUT_DIR / f"{name}-{n}.json").exists():
        n += 1
    return OUTPUT_DIR / f"{name}-{n}.json"


def run(target: str) -> int:
    """Read data/<target>.txt, normalize, and write output/<target>-<N>.json."""
    target_path = DATA_DIR / f"{target}.txt"
    if not target_path.is_file():
        print(f"error: target not found: {target_path}", file=sys.stderr)
        return 1

    records, warnings = parse_text(target_path.read_text(encoding="utf-8"))
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = _next_output_path(target)
    output_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"parsed {len(records)} verses: {target_path} -> {output_path}", file=sys.stderr)
    return 0
