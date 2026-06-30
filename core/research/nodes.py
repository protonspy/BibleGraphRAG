"""Graph nodes and the LLM/GraphRAG helpers behind them.

Three graph nodes (write_brief → research_round → synthesize) plus the per-query pipeline that
research_round runs sequentially for each planned query: classify (reuse core.graph.route) → search
(core.graph.search, captured, not streamed) → distil. Searches run sequentially on purpose: GraphRAG's
load_config os.chdir's into rag_root, so concurrent searches would race on the process cwd — parallel
fan-out (Send) is the next step, gated on removing that chdir. The model split is the main cost lever:
the small model distils/classifies (runs breadth×depth times), the main model plans and synthesizes.
"""
from __future__ import annotations

import math
import sys

from pydantic import BaseModel, Field

from core.config import env
from core.llm import structured_model
from core.research import prompts
from core.research.state import Learning, ResearchState, SubQuery

# Cost governors. The classifier already biases away from global; these are hard caps on top.
MAX_GLOBAL_CALLS = 3          # most expensive method (map-reduce over every community report)
ANSWER_CHAR_CAP = 12_000      # trim a GraphRAG answer before the small model distils it
RESEARCH_RESPONSE_TYPE = "List of 3-7 concise points"  # terse GraphRAG output to keep tokens low
N_LEARNINGS = 3
N_FOLLOWUPS = 3


# --- structured-output schemas -----------------------------------------------------------

class _SubQuery(BaseModel):
    query: str = Field(description="A self-contained search query answerable from the Bible knowledge graph.")
    goal: str = Field(description="What this query should establish, and what to investigate next from its results.")


class _Queries(BaseModel):
    queries: list[_SubQuery] = Field(description="The planned search queries.")


class _Processed(BaseModel):
    learnings: list[str] = Field(description="Dense, self-contained findings with scripture refs and named entities.")
    follow_up_questions: list[str] = Field(description="Specific questions to research next.")


class _Report(BaseModel):
    report_markdown: str = Field(description="The final research report in Markdown.")


class _Answer(BaseModel):
    exact_answer: str = Field(description="The concise final answer, no other text.")


def _small() -> str:
    return env.small_model or env.llm_model


# --- LLM helpers -------------------------------------------------------------------------

def generate_queries(seed: str, n: int, learnings: list[str]) -> list[SubQuery]:
    """Plan up to `n` distinct sub-queries from `seed`, informed by what's been learned (main model)."""
    learned = "\n".join(f"- {t}" for t in learnings) if learnings else "(none yet)"
    prompt = prompts.GENERATE_QUERIES.format(n=n, seed=seed, learnings=learned)
    model = structured_model(_Queries, model=env.llm_model)
    res: _Queries = model.invoke([("system", prompts.system_prompt()), ("human", prompt)])  # type: ignore[assignment]
    return [{"query": q.query, "goal": q.goal} for q in res.queries[:n]]


def process_result(query: str, answer: str) -> tuple[list[str], list[str]]:
    """Distil one GraphRAG answer into ≤N dense learnings + follow-up questions (small model).

    This is the compression step: the verbose answer is read once here and discarded; only the
    learnings propagate. The answer is trimmed first so a long global-search response can't blow up
    the distillation cost.
    """
    contents = answer[:ANSWER_CHAR_CAP]
    prompt = prompts.PROCESS_RESULT.format(
        query=query, n_learnings=N_LEARNINGS, n_follow=N_FOLLOWUPS, contents=contents)
    model = structured_model(_Processed, model=_small())
    res: _Processed = model.invoke([("system", prompts.system_prompt()), ("human", prompt)])  # type: ignore[assignment]
    return res.learnings[:N_LEARNINGS], res.follow_up_questions[:N_FOLLOWUPS]


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
        model = structured_model(_Answer, model=env.llm_model)
        res: _Answer = model.invoke([  # type: ignore[assignment]
            ("system", prompts.system_prompt()),
            ("human", prompts.FINAL_ANSWER.format(prompt=question, learnings=block))])
        return res.exact_answer
    model = structured_model(_Report, model=env.llm_model)
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
    followups: list[str] = []
    global_calls = state.get("global_calls", 0)

    for q in queries:
        mode = classify_method(q["query"])
        if mode == "global" and global_calls >= MAX_GLOBAL_CALLS:
            mode = "drift"  # cost guard: keep the big-picture intent but skip the most expensive method

        try:
            answer = graphrag_answer(mode, q["query"])
            if not answer.strip():
                continue
            learnings, follow = process_result(q["query"], answer)
        except Exception as exc:  # one method failing must not abort the whole run (dzhng-style resilience)
            print(f"[research] {mode} search failed for {q['query']!r} "
                  f"({type(exc).__name__}: {exc}); skipping", file=sys.stderr, flush=True)
            continue

        if mode == "global":  # only count a search that actually ran
            global_calls += 1
        new_learnings.extend({"text": t, "mode": mode, "query": q["query"]} for t in learnings)  # type: ignore
        new_sources.append(f"{mode}: {q['query']}")
        followups.extend(follow)

    next_seed = "\n".join(
        [f"Goal: {q['goal']}" for q in queries] + [f"Follow-up: {f}" for f in followups]
    ).strip()

    return {
        "frontier": queries,
        "learnings": new_learnings,
        "sources": new_sources,
        "pending": followups,
        "seed": next_seed,
        "depth": state["depth"] - 1,
        "breadth": max(1, math.ceil(state["breadth"] / 2)),
        "global_calls": global_calls,
    }


def synthesize(state: ResearchState) -> dict:
    """Final node: compose the report/answer from accumulated learnings and append the source list."""
    texts = [lr["text"] for lr in state.get("learnings", [])]
    out = synthesize_output(state["question"], texts, state.get("output_mode", "report"))
    if state.get("output_mode") == "report":
        sources = state.get("sources", [])
        if sources:
            out += "\n\n## Sources\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(sources, 1))
    return {"report": out}
