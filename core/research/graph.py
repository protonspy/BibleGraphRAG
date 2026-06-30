"""The deep_researcher graph: plan → (classify → search → distil)* → synthesize.

A thin LangGraph StateGraph with a round loop that expresses dzhng's recursive breadth×depth research
as breadth-first rounds: write_brief seeds the first round, research_round runs one round and decides
(via route_round) whether another round is warranted, and synthesize composes the final output. The
per-query fan-out (Send) and a researcher subgraph are the next iteration; v1 runs each round's
queries sequentially (see core.research.nodes for why).
"""
from __future__ import annotations

import sys
from typing import Literal

from langgraph.graph import END, START, StateGraph

from core.research.nodes import research_round, synthesize, write_brief
from core.research.state import ResearchState


def route_round(state: ResearchState) -> Literal["research_round", "synthesize"]:
    """Loop into another round while depth remains and there are follow-ups to chase; else synthesize."""
    if state.get("depth", 0) > 0 and state.get("pending"):
        return "research_round"
    return "synthesize"


def build_graph():
    """Compile the deep_researcher StateGraph."""
    builder = StateGraph(ResearchState)
    builder.add_node("write_brief", write_brief)
    builder.add_node("research_round", research_round)
    builder.add_node("synthesize", synthesize)

    builder.add_edge(START, "write_brief")
    builder.add_edge("write_brief", "research_round")
    builder.add_conditional_edges(
        "research_round", route_round,
        {"research_round": "research_round", "synthesize": "synthesize"})
    builder.add_edge("synthesize", END)
    return builder.compile()


def run(
    question: str,
    breadth: int = 4,
    depth: int = 2,
    output_mode: str = "report",
    dry_run: bool = False,
) -> int:
    """Run deep research over the built index and print the final report/answer to stdout.

    `breadth` = queries planned per round (halved each round); `depth` = number of rounds. Returns a
    process exit code. `dry_run` prints the graph structure and the plan without calling the LLM/index.
    """
    graph = build_graph()
    if dry_run:
        print(graph.get_graph().draw_mermaid())
        print(f"[plan] question={question!r} breadth={breadth} depth={depth} mode={output_mode}",
              file=sys.stderr)
        return 0

    initial: ResearchState = {
        "question": question,
        "output_mode": output_mode,  # type: ignore[typeddict-item]
        "depth": depth,
        "breadth": breadth,
        "learnings": [],
        "sources": [],
        "global_calls": 0,
    }
    # Each round is a handful of super-steps; cap generously so depth (≤5) never trips the limit.
    final = graph.invoke(initial, {"recursion_limit": 50})
    print(final.get("report", ""))
    return 0
