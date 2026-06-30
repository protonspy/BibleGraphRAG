"""Graph nodes and the LLM/GraphRAG helpers behind them.

Three graph nodes (write_brief → research_round → synthesize) plus the per-query pipeline that
research_round runs sequentially for each planned query, over two complementary sources: the Bible
knowledge graph — classify (reuse core.graph.route, restricted to local/global — drift is too slow
per query) → search (core.graph.search, captured, not streamed) → distil — and (when `scholar` is on)
OpenAlex scholarly literature, searched on a per-query scholar_query and distilled the same way. Both
feed the same compression step; OpenAlex's Open-Access works also become the report's real citations.
Searches run sequentially on purpose: GraphRAG's load_config os.chdir's into rag_root, so concurrent
searches would race on the process cwd — parallel fan-out (Send) is the next step, gated on removing
that chdir. The model split is the main cost lever: the small model distils/classifies (runs
breadth×depth times, now twice per query when scholar is on), the main model plans and synthesizes.
"""
from __future__ import annotations

import asyncio
import math
import sys
from itertools import zip_longest

from pydantic import BaseModel, Field

from core.config import env
from core.llm import structured_model
from core.research import fulltext, openalex, prompts
from core.research.openalex import Work
from core.research.state import Learning, ResearchState, SubQuery

# Cost governors. The classifier already biases away from global; these are hard caps on top.
RESEARCH_METHODS = ("local", "global")  # the loop routes only to these — drift is too slow per query
MAX_GLOBAL_CALLS = 3          # most expensive method (map-reduce over every community report)
MAX_DEEP_READS = 5            # full-text article reads per round (the expensive scholarly unit)
DEEP_READ_CONCURRENCY = 6     # parallel article reads in flight (politeness + token rate limits)
ANSWER_CHAR_CAP = 12_000      # trim a GraphRAG answer before the small model distils it
RESEARCH_RESPONSE_TYPE = "List of 3-7 concise points"  # terse GraphRAG output to keep tokens low
N_LEARNINGS = 3
N_FOLLOWUPS = 3
N_REFERENCES = 5              # key references pulled from each article (validation / citation chain)


# --- structured-output schemas -----------------------------------------------------------

class _SubQuery(BaseModel):
    query: str = Field(description="A self-contained search query answerable from the Bible knowledge graph.")
    goal: str = Field(description="What this query should establish, and what to investigate next from its results.")
    scholar_query: str = Field(
        description="The same information need as academic search keywords for a scholarly literature "
                    "database (English, domain terms — figures, places, periods, concepts; no question words).")


class _Queries(BaseModel):
    queries: list[_SubQuery] = Field(description="The planned search queries.")


class _Processed(BaseModel):
    learnings: list[str] = Field(description="Dense, self-contained findings with scripture refs and named entities.")
    follow_up_questions: list[str] = Field(description="Specific questions to research next.")


class _Reference(BaseModel):
    citation: str = Field(description="A source the article relies on (author/year/title as it appears).")
    note: str = Field(description="What this reference supports in the article — for validation/tracing.")


class _ArticleRead(BaseModel):
    relevant: bool = Field(description="Whether the article genuinely addresses the brief (not just keyword-adjacent).")
    summary: str = Field(default="", description="Cohesive synthesis of the article's central argument and findings.")
    references: list[_Reference] = Field(default_factory=list, description="Key sources the article itself cites.")
    research_queries: list[str] = Field(
        default_factory=list, description="Specific new research questions this article opens up.")


class _Report(BaseModel):
    report_markdown: str = Field(description="The final research report in Markdown.")


class _Answer(BaseModel):
    exact_answer: str = Field(description="The concise final answer, no other text.")


def _small() -> str:
    return env.research_small_model or env.research_model


# --- LLM helpers -------------------------------------------------------------------------

def generate_queries(seed: str, n: int, learnings: list[str]) -> list[SubQuery]:
    """Plan up to `n` distinct sub-queries from `seed`, informed by what's been learned (main model)."""
    learned = "\n".join(f"- {t}" for t in learnings) if learnings else "(none yet)"
    prompt = prompts.GENERATE_QUERIES.format(n=n, seed=seed, learnings=learned)
    model = structured_model(_Queries, model=env.research_model)
    res: _Queries = model.invoke([("system", prompts.system_prompt()), ("human", prompt)])  # type: ignore[assignment]
    return [{"query": q.query, "goal": q.goal, "scholar_query": q.scholar_query} for q in res.queries[:n]]


def process_result(query: str, answer: str) -> tuple[list[str], list[str]]:
    """Distil one GraphRAG answer into ≤N dense learnings + follow-up questions (small model).

    This is the compression step: the verbose answer is read once here and discarded; only the
    learnings propagate. The answer is trimmed first so a long response can't blow up the cost.
    """
    contents = answer[:ANSWER_CHAR_CAP]
    prompt = prompts.PROCESS_RESULT.format(
        query=query, n_learnings=N_LEARNINGS, n_follow=N_FOLLOWUPS, contents=contents)
    model = structured_model(_Processed, model=_small())
    res: _Processed = model.invoke([("system", prompts.system_prompt()), ("human", prompt)])  # type: ignore[assignment]
    return res.learnings[:N_LEARNINGS], res.follow_up_questions[:N_FOLLOWUPS]


def _work_id(work: Work) -> str:
    """Stable id for dedup across queries and rounds — DOI when present, else the OA url, else title."""
    return work["doi"] or work["oa_url"] or work["title"]


def _work_meta(work: Work) -> str:
    """One-line metadata header for the reader prompt (no abstract — the contents carry the text)."""
    meta = f"{work['authors']} ({work['year'] or 'n.d.'}). {work['title']}."
    return f"{meta} {work['venue']}." if work["venue"] else meta


def _gather_candidates(queries: list[SubQuery]) -> list[tuple[Work, str]]:
    """One OpenAlex search per query; return (work, originating_query) pairs, deduped by work id."""
    seen: set[str] = set()
    out: list[tuple[Work, str]] = []
    for q in queries:
        for w in openalex.search_works(q.get("scholar_query") or q["query"]):
            wid = _work_id(w)
            if wid in seen:
                continue
            seen.add(wid)
            out.append((w, q["query"]))
    return out


def _select_for_deep_read(
        candidates: list[tuple[Work, str]], already: set[str], cap: int) -> list[tuple[Work, str]]:
    """Pick ≤`cap` works to read in full: round-robin across queries (breadth), skipping already-read."""
    by_query: dict[str, list[tuple[Work, str]]] = {}
    for w, q in candidates:
        if _work_id(w) in already:
            continue
        by_query.setdefault(q, []).append((w, q))
    interleaved = [pair for row in zip_longest(*by_query.values()) for pair in row if pair]
    return interleaved[:cap]


async def _read_one(work: Work, query: str, brief: str) -> dict | None:
    """Acquire and read ONE article in full; distil it to summary + references + follow-up queries.

    Returns None when the article yields no text or the reader judges it irrelevant to the brief.
    """
    text, provenance = await asyncio.to_thread(fulltext.fetch_fulltext, work)
    if not text.strip():
        return None
    label = "full text" if provenance == fulltext.READER else "abstract (full text was unavailable)"
    prompt = prompts.READ_ARTICLE.format(
        provenance=label, query=query, brief=brief, meta=_work_meta(work),
        n_refs=N_REFERENCES, n_queries=N_FOLLOWUPS, contents=text)
    model = structured_model(_ArticleRead, model=_small())
    res: _ArticleRead = await model.ainvoke(  # type: ignore[assignment]
        [("system", prompts.system_prompt()), ("human", prompt)])
    if not res.relevant or not res.summary.strip():
        return None
    return {
        "query": query,
        "provenance": provenance,
        "summary": res.summary,
        "citation": openalex.citation(work),
        "references": [f"{r.citation} — {r.note}" for r in res.references[:N_REFERENCES]],
        "research_queries": res.research_queries[:N_FOLLOWUPS],
        "work_id": _work_id(work),
    }


async def _read_articles(selected: list[tuple[Work, str]], brief: str) -> list[dict]:
    """Read the selected articles concurrently (bounded), dropping ones that fail or aren't relevant."""
    sem = asyncio.Semaphore(DEEP_READ_CONCURRENCY)

    async def guarded(work: Work, query: str) -> dict | None:
        async with sem:
            try:
                return await _read_one(work, query, brief)
            except Exception as exc:  # one article failing must not abort the batch (dzhng-style)
                print(f"[research] deep read failed for {work['title']!r} "
                      f"({type(exc).__name__}: {exc}); skipping", file=sys.stderr, flush=True)
                return None

    results = await asyncio.gather(*(guarded(w, q) for w, q in selected))
    return [r for r in results if r]


def graphrag_answer(mode: str, query: str) -> str:
    """Run the chosen GraphRAG method and return its answer as a string (not streamed to stdout)."""
    from core.graph import search

    if mode == "global":
        return search.global_search(query, response_type=RESEARCH_RESPONSE_TYPE, stream=False)
    if mode == "drift":
        return search.drift_search(query, response_type=RESEARCH_RESPONSE_TYPE, stream=False)
    if mode == "basic":
        return search.basic_search(query, response_type=RESEARCH_RESPONSE_TYPE, stream=False)
    return search.local_search(query, response_type=RESEARCH_RESPONSE_TYPE, stream=False)


def synthesize_output(question: str, learnings: list[str], output_mode: str) -> str:
    """Write the final report (or concise answer) from the accumulated learnings (main model)."""
    block = "\n".join(f"<learning>\n{t}\n</learning>" for t in learnings) or "(no learnings gathered)"
    if output_mode == "answer":
        model = structured_model(_Answer, model=env.research_model)
        res: _Answer = model.invoke([  # type: ignore[assignment]
            ("system", prompts.system_prompt()),
            ("human", prompts.FINAL_ANSWER.format(prompt=question, learnings=block))])
        return res.exact_answer
    model = structured_model(_Report, model=env.research_model)
    rep: _Report = model.invoke([  # type: ignore[assignment]
        ("system", prompts.system_prompt()),
        ("human", prompts.FINAL_REPORT.format(prompt=question, learnings=block))])
    return rep.report_markdown


# --- graph nodes -------------------------------------------------------------------------

def write_brief(state: ResearchState) -> dict:
    """Entry node: establish the research brief and the first round's planning seed.

    For now the brief is the question verbatim; this is where a clarify/restate step would hook in.
    """
    return {"brief": state["question"], "seed": state["question"]}


def research_round(state: ResearchState) -> dict:
    """One BFS round: plan queries from the seed, then classify→search→distil each, sequentially.

    Returns the new learnings/sources (appended via the state reducers), the next seed built from this
    round's follow-up questions, and the decremented depth / halved breadth for the loop.
    """
    from core.graph.route import classify_method

    known = [lr["text"] for lr in state.get("learnings", [])]
    queries = generate_queries(state["seed"], state["breadth"], known)

    new_learnings: list[Learning] = []
    new_sources: list[str] = []
    new_citations: list[str] = []
    new_references: list[str] = []
    read_ids: list[str] = []
    followups: list[str] = []
    global_calls = state.get("global_calls", 0)
    scholar = state.get("scholar", True)

    # Source 1 — the Bible knowledge graph (what the text says). Sequential: GraphRAG chdir's into
    # rag_root, so concurrent graph searches would race on the process cwd.
    for q in queries:
        mode = classify_method(q["query"], allowed=RESEARCH_METHODS)
        if mode == "global" and global_calls >= MAX_GLOBAL_CALLS:
            mode = "local"  # cost guard: drop to local once global is capped (was 'drift', the slow one)
        try:
            answer = graphrag_answer(mode, q["query"])
            if answer.strip():
                learnings, follow = process_result(q["query"], answer)
                if mode == "global":  # only count a search that actually produced an answer
                    global_calls += 1
                new_learnings.extend(
                    {"text": t, "source": "graph", "mode": mode, "query": q["query"]} for t in learnings) # noqa
                new_sources.append(f"{mode}: {q['query']}")
                followups.extend(follow)
        except Exception as exc:  # one source failing must not abort the run (dzhng-style resilience)
            print(f"[research] {mode} search failed for {q['query']!r} "
                  f"({type(exc).__name__}: {exc}); skipping", file=sys.stderr, flush=True)

    # Source 2 — scholarly deep reads (parallel, off the GraphRAG cwd, so unaffected by the chdir):
    # gather OA candidates across the round's queries, pick the top few, read each in full and distil it
    # to a summary + the references that validate it + new research queries it opens up.
    if scholar:
        already = set(state.get("read_works", []))
        selected = _select_for_deep_read(_gather_candidates(queries), already, MAX_DEEP_READS)
        brief = state.get("brief") or state["seed"]
        for r in asyncio.run(_read_articles(selected, brief)):
            new_learnings.append(
                {"text": r["summary"], "source": "openalex", "mode": f"deep-read/{r['provenance']}",
                 "query": r["query"]})
            new_citations.append(r["citation"])
            new_references.extend(r["references"])
            followups.extend(r["research_queries"])
            new_sources.append(f"deep-read: {r['query']}")
            read_ids.append(r["work_id"])

    next_seed = "\n".join(
        [f"Goal: {q['goal']}" for q in queries] + [f"Follow-up: {f}" for f in followups]
    ).strip()

    return {
        "frontier": queries,
        "learnings": new_learnings,
        "sources": new_sources,
        "citations": new_citations,
        "lit_references": new_references,
        "read_works": read_ids,
        "pending": followups,
        "seed": next_seed,
        "depth": state["depth"] - 1,
        "breadth": max(1, math.ceil(state["breadth"] / 2)),
        "global_calls": global_calls,
    }


def _dedup(items: list[str]) -> list[str]:
    """Order-preserving dedup (the same paper/query recurs across queries and rounds)."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def synthesize(state: ResearchState) -> dict:
    """Final node: compose the report/answer, then append scholarly references and the search trail."""
    texts = [lr["text"] for lr in state.get("learnings", [])]
    out = synthesize_output(state["question"], texts, state.get("output_mode", "report"))
    if state.get("output_mode") == "report":
        citations = _dedup(state.get("citations", []))
        if citations:  # real, Open-Access scholarly works (OpenAlex) — the verifiable bibliography
            out += "\n\n## References\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(citations, 1))
        references = _dedup(state.get("lit_references", []))
        if references:  # works the read articles themselves cite — for validation / further reading
            out += "\n\n## Cited in the literature\n" + "\n".join(
                f"{i}. {r}" for i, r in enumerate(references, 1))
        sources = _dedup(state.get("sources", []))
        if sources:  # the internal search trail (which graph method answered which sub-query)
            out += "\n\n## Search trail\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(sources, 1))
    return {"report": out}
