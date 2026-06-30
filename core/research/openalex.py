"""OpenAlex /works as a second research source: scholarly literature beside the Bible graph.

Per sub-query the deep researcher runs a graph search (what the text says) and an OpenAlex search
(what the scholarship says). This module owns the HTTP call (httpx), the Open-Access filter (only works
with a reachable oa_url come back, so every cited work is verifiable), abstract reconstruction from
OpenAlex's inverted index, and the compact rendering the distiller reads. Network/parse failures
return [] so a missing source never aborts a research round.
"""
from __future__ import annotations

import sys
from typing import TypedDict

import httpx

from core.config import env

# Fields requested from /works — `select` keeps the payload (and the distilled token count) small.
_SELECT = (
    "id", "doi", "title", "publication_year", "cited_by_count",
    "authorships", "primary_location", "open_access", "best_oa_location",
    "abstract_inverted_index",
)
_TIMEOUT = 20      # seconds; OpenAlex is usually fast, but never let it hang a round
_ABSTRACT_CAP = 1500  # trim each abstract so a handful of works stays well under the distiller's cap


class Work(TypedDict):
    """One scholarly work, normalized for the distiller and for citation."""

    title: str
    authors: str           # "First Author, Second Author, et al."
    year: int | None
    venue: str             # journal/source display name ("" if unknown)
    cited_by_count: int
    doi: str               # bare DOI url, or "" if none
    oa_url: str            # open-access url (always set — we filter on it)
    pdf_url: str           # direct OA PDF when OpenAlex knows one ("" otherwise) — the deep-read entry
    landing_page_url: str  # OA landing page ("" if none) — fallback target for full-text acquisition
    abstract: str          # reconstructed plain text (may be "")


def _user_agent() -> str:
    """Polite-pool User-Agent; includes the configured mailto when set."""
    base = "BibleGraphRAG/0.1 (deep-researcher)"
    return f"{base} mailto:{env.openalex_mailto}" if env.openalex_mailto else base


def _reconstruct_abstract(inverted: dict | None) -> str:
    """Rebuild a plain-text abstract from OpenAlex's {word: [positions]} inverted index."""
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = [
        (i, word) for word, idxs in inverted.items() for i in idxs
    ]
    positions.sort()
    text = " ".join(word for _, word in positions)
    return text[:_ABSTRACT_CAP]


def _authors(authorships: list[dict] | None, limit: int = 3) -> str:
    """Render up to `limit` author names, '… et al.' beyond that."""
    names = [a.get("author", {}).get("display_name", "") for a in (authorships or [])]
    names = [n for n in names if n]
    if not names:
        return "Unknown"
    head = ", ".join(names[:limit])
    return f"{head}, et al." if len(names) > limit else head


def search_works(query: str, per_page: int | None = None, open_access_only: bool = True) -> list[Work]:
    """Search OpenAlex /works for `query`; return up to `per_page` normalized, OA-filtered works.

    The Open-Access filter is applied server-side (filter=is_oa:true) so only works with a reachable
    oa_url come back — those are the ones we can cite as verifiable sources. Sorted by relevance.
    Returns [] on any network/parse error (logged to stderr); the caller still guards per-query too.
    """
    per_page = per_page or env.openalex_per_page
    params = {
        "search": query,
        "per_page": str(per_page),
        "select": ",".join(_SELECT),
        "sort": "relevance_score:desc",
    }
    if open_access_only:
        params["filter"] = "is_oa:true"
    if env.openalex_mailto:
        params["mailto"] = env.openalex_mailto

    try:
        resp = httpx.get(
            f"{env.openalex_base_url}/works",
            params=params,
            headers={"User-Agent": _user_agent()},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # network, HTTP status, or JSON parse — a missing source never aborts a round
        print(f"[openalex] request failed for {query!r} ({type(exc).__name__}: {exc})",
              file=sys.stderr, flush=True)
        return []

    works: list[Work] = []
    for r in payload.get("results", []):
        oa = r.get("open_access") or {}
        oa_url = oa.get("oa_url") or ""
        if open_access_only and not oa_url:
            continue  # belt-and-suspenders: keep only works we can point a reader to
        src = (r.get("primary_location") or {}).get("source") or {}
        best = r.get("best_oa_location") or {}
        works.append({
            "title": r.get("title") or "(untitled)",
            "authors": _authors(r.get("authorships")),
            "year": r.get("publication_year"),
            "venue": src.get("display_name") or "",
            "cited_by_count": r.get("cited_by_count") or 0,
            "doi": r.get("doi") or "",
            "oa_url": oa_url,
            "pdf_url": best.get("pdf_url") or "",
            "landing_page_url": best.get("landing_page_url") or "",
            "abstract": _reconstruct_abstract(r.get("abstract_inverted_index")),
        })
    return works


def format_works(works: list[Work]) -> str:
    """Render works as a compact block for the distiller: one <work> per paper, metadata + abstract."""
    blocks = []
    for w in works:
        meta = f"{w['authors']} ({w['year'] or 'n.d.'}). {w['title']}."
        if w["venue"]:
            meta += f" {w['venue']}."
        abstract = w["abstract"] or "(no abstract available)"
        blocks.append(f"<work>\n{meta}\nAbstract: {abstract}\n</work>")
    return "\n".join(blocks)


def citation(w: Work) -> str:
    """A one-line reference for the report's bibliography (author, year, title, venue, DOI/OA url)."""
    venue = f" {w['venue']}." if w["venue"] else ""
    ref = w["doi"] or w["oa_url"]
    return f"{w['authors']} ({w['year'] or 'n.d.'}). {w['title']}.{venue} {ref}".strip()
