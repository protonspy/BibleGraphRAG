"""CLI wiring — registers subcommands and dispatches to pipeline handlers."""
from __future__ import annotations

import argparse

from core.cost import estimate as estimate_step
from core.graph import build as build_step
from core.graph import query as query_step
from core.pipe import parser as parser_step
from core.pipe import pericope as pericope_step
from core.research import graph as research_step


def _parse_chapters(spec: str | None) -> set[int] | None:
    """Parse a chapters spec like '1-3', '1,3', or '1-3,5' into a set of ints."""
    if not spec:
        return None
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out or None


def build_parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="bgr", description="BibleGraphRAG CLI")
    sub = cli.add_subparsers(dest="command", required=True)

    p_parser = sub.add_parser("parser", help="Normalize raw text into structured JSON")
    p_parser.add_argument("--target", required=True, help="Input name, resolved to data/<name>.txt")
    p_parser.set_defaults(handler=lambda args: parser_step.run(args.target))

    p_est = sub.add_parser("estimate", help="Estimate USD cost to process a corpus through the pipeline")
    p_est.add_argument("--target", required=True, help="Input name, resolved to data/<name>.txt")
    p_est.add_argument("--input-factor", type=float, default=estimate_step.DEFAULT_INPUT_FACTOR,
                       help="LLM input tokens per corpus token (prompts + context repeated per call)")
    p_est.add_argument("--output-ratio", type=float, default=estimate_step.DEFAULT_OUTPUT_RATIO,
                       help="LLM output tokens as a fraction of corpus tokens")
    p_est.add_argument("--embed-factor", type=float, default=estimate_step.DEFAULT_EMBED_FACTOR,
                       help="Embedding tokens as a multiple of corpus tokens")
    p_est.add_argument("--model", default=None, help="Override the main LLM model id (default: LLM_MODEL from .env)")
    p_est.add_argument("--no-fetch", action="store_true", help="Skip live OpenRouter pricing; use builtin table")
    p_est.set_defaults(handler=lambda args: estimate_step.run(
        args.target,
        input_factor=args.input_factor,
        output_ratio=args.output_ratio,
        embed_factor=args.embed_factor,
        model_override=args.model,
        fetch=not args.no_fetch,
    ))

    p_peri = sub.add_parser("pericope", help="Segment a parsed corpus into pericopes (LLM) and cache them")
    p_peri.add_argument("--target", required=True, help="Corpus name, resolved to output/<name>[-N].json")
    p_peri.add_argument("--book", default=None, help="Only segment this book (e.g. Genesis)")
    peri_ch = p_peri.add_mutually_exclusive_group()
    peri_ch.add_argument("--chapter", type=int, default=None, help="Only segment this chapter number")
    peri_ch.add_argument("--chapters", default=None, help="Only segment these chapters: '1-3', '1,3', '1-3,5'")
    p_peri.add_argument("--limit", type=int, default=None, help="Cap the number of chapters to segment")
    p_peri.add_argument("--model", default=None, help="Override the LLM model id (default: LLM_MODEL from .env)")
    p_peri.add_argument("--force", action="store_true", help="Re-segment chapters already in the cache")
    p_peri.add_argument("--dry-run", action="store_true", help="Show the plan without calling the LLM")
    p_peri.set_defaults(handler=lambda args: pericope_step.run(
        args.target,
        book=args.book,
        chapter=args.chapter,
        chapters=_parse_chapters(args.chapters),
        limit=args.limit,
        model=args.model,
        force=args.force,
        dry_run=args.dry_run,
    ))

    p_build = sub.add_parser("build", help="Build the GraphRAG index (parquet + lancedb) from a parsed corpus")
    p_build.add_argument("--target", required=True, help="Corpus name, resolved to output/<name>[-N].json")
    p_build.add_argument("--book", default=None, help="Only include this book (e.g. Genesis)")
    build_ch = p_build.add_mutually_exclusive_group()
    build_ch.add_argument("--chapter", type=int, default=None, help="Only include this chapter number")
    build_ch.add_argument("--chapters", default=None, help="Only include these chapters: '1-3', '1,3', '1-3,5'")
    p_build.add_argument("--limit", type=int, default=None, help="Cap the number of documents (pericopes/chapters)")
    p_build.add_argument("--no-pericope", dest="pericope", action="store_false", default=True,
                         help="One document per chapter (default: per pericope, LLM-segmented and cached on demand)")
    p_build.add_argument("--skip-index", action="store_true",
                         help="Only prepare the input CSV; don't run `graphrag index`")
    p_build.add_argument("--verbose", action="store_true", help="Verbose `graphrag index` output")
    p_build.add_argument("--dry-run", action="store_true", help="Show the plan without calling the LLM/GraphRAG")
    p_build.set_defaults(handler=lambda args: build_step.run(
        args.target,
        book=args.book,
        chapter=args.chapter,
        chapters=_parse_chapters(args.chapters),
        limit=args.limit,
        dry_run=args.dry_run,
        pericope=args.pericope,
        skip_index=args.skip_index,
        verbose=args.verbose,
    ))

    p_query = sub.add_parser("query", help="Query the index (auto-routed, or global / local / drift / basic)")
    p_query.add_argument("question", help="The question to ask the graph")
    p_query.add_argument("--method", "-m", default=None, choices=list(query_step.METHODS),
                         help="Search method (default: auto — an LLM classifies the question). "
                              "global = sensemaking over community reports; local = entity vector search + "
                              "neighbourhood; drift = community priming + entity drill-down; "
                              "basic = plain vector RAG over text units")
    p_query.add_argument("--community-level", type=int, default=None,
                         help="Max Leiden level of community reports to use (global / drift search)")
    p_query.add_argument("--response-type", default=None,
                         help="Free-form response shape, e.g. 'single paragraph', 'bullet points'")
    p_query.add_argument("--verbose", action="store_true",
                         help="Per-report (global) / per-entity (local) progress detail on stderr")
    p_query.set_defaults(handler=lambda args: query_step.run(
        args.question,
        method=args.method,
        community_level=args.community_level,
        response_type=args.response_type,
        verbose=args.verbose,
    ))

    p_research = sub.add_parser(
        "research",
        help="Deep research over the index: plan sub-queries → classify → search → distil → synthesize")
    p_research.add_argument("question", help="The research question")
    p_research.add_argument("--breadth", type=int, default=4,
                            help="Sub-queries planned per round (halves each round); default 4")
    p_research.add_argument("--depth", type=int, default=3,
                            help="Number of research rounds (follow-ups drive the next round); default 3")
    p_research.add_argument("--answer", dest="output_mode", action="store_const", const="answer",
                            default="report", help="Produce a concise answer instead of a full report")
    p_research.add_argument("--no-scholar", dest="scholar", action="store_false", default=True,
                            help="Disable OpenAlex scholarly augmentation (research the graph only)")
    p_research.add_argument("--dry-run", action="store_true",
                            help="Print the graph structure and plan without calling the LLM/index")
    p_research.set_defaults(handler=lambda args: research_step.run(
        args.question,
        breadth=args.breadth,
        depth=args.depth,
        output_mode=args.output_mode,
        scholar=args.scholar,
        dry_run=args.dry_run,
    ))

    return cli


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)
