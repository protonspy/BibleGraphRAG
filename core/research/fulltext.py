"""Full-text acquisition for the deep-read tier — turn an OA work into readable plain text.

The deep researcher reads a handful of the most relevant works in full (not just their abstract). OA
links are hostile to robots: a direct fetch of a repository PDF often 403/405s, many `oa_url`s are
landing pages rather than the file, and some are scanned (image-only) PDFs that need OCR. Rather than
own that mess (fetch quirks, PDF text extraction, OCR), acquisition goes through a reader service — a
browser-emulating proxy (Jina Reader by default) that handles redirects, bot walls, HTML extraction
and server-side PDF/OCR, returning clean text. We just pick the best target and degrade gracefully:

    1. reader service over the work's targets (direct PDF → OA url → landing page), first clean hit wins.
    2. abstract — whatever OpenAlex already gave us, so a work we can't read in full is never nothing.

`fetch_fulltext` returns (text, provenance) so the caller (and the report) can tell a full read from a
degraded one. Every failure is swallowed to a fallback: acquisition must never abort a research round.
"""
from __future__ import annotations

import sys

import httpx

from core.config import env
from core.research.openalex import Work

MIN_FULLTEXT_CHARS = 800     # below this a "full text" is a stub/block page — treat acquisition as failed
FULLTEXT_CAP = 60_000        # ~15k tokens; generous within flash's 1M context, caps a pathological PDF
_TIMEOUT = 40                # the reader emulates a browser + may OCR a PDF — slower than the bare API
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BibleGraphRAG/0.1 (deep-researcher)"

# Markers the reader returns when the *target* sits behind a captcha / bot wall — a short stub, not the
# article. These can clear the length threshold (a "Human Verification" page is ~900 chars), so reject
# them explicitly instead of letting the stub pass as full text.
_BLOCK_MARKERS = (
    "human verification", "just a moment", "enable javascript", "captcha",
    "access denied", "are you a robot", "cloudflare",
)

# Provenance tags. The caller surfaces these so a degraded (abstract-only) read stays visible.
READER = "reader"
ABSTRACT = "abstract"


def _looks_blocked(text: str) -> bool:
    head = text[:500].lower()
    return any(m in head for m in _BLOCK_MARKERS)


def _ok(text: str) -> bool:
    """Real full text: clears the stub threshold and isn't a bot-wall page the reader echoed back."""
    return bool(text) and len(text.strip()) >= MIN_FULLTEXT_CHARS and not _looks_blocked(text)


def _cap(text: str) -> str:
    return text.strip()[:FULLTEXT_CAP]


def _reader_fetch(url: str) -> str:
    """Fetch `url` through the configured reader service (browser-emulating proxy); '' if unavailable."""
    if not env.reader_service_url:
        return ""
    try:
        resp = httpx.get(
            f"{env.reader_service_url}{url}", timeout=_TIMEOUT,
            headers={"User-Agent": _UA, "Accept": "text/plain"})
        resp.raise_for_status()
        return resp.text
    except Exception as exc:  # network / HTTP status — never abort a round over one source
        print(f"[fulltext] reader fetch failed for {url!r} ({type(exc).__name__}: {exc})",
              file=sys.stderr, flush=True)
        return ""


def fetch_fulltext(work: Work) -> tuple[str, str]:
    """Acquire the best available text for `work` and how we got it (READER or ABSTRACT).

    Hands the reader the work's targets in fidelity order (direct PDF, then OA url, then landing page) —
    which one clears the bot wall varies by host — and takes the first clean result. Synchronous on
    purpose: callers fan this out with asyncio.to_thread.
    """
    seen: set[str] = set()
    for url in (work["pdf_url"], work["oa_url"], work["landing_page_url"]):
        if not url or url in seen:
            continue
        seen.add(url)
        text = _reader_fetch(url)
        if _ok(text):
            return _cap(text), READER

    # Degrade — the abstract is always something, never nothing.
    return work["abstract"], ABSTRACT
