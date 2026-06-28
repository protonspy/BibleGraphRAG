"""CLI wiring — registers subcommands and dispatches to pipeline handlers."""
from __future__ import annotations

import argparse

from core.cost import estimate as estimate_step
from core.graph import build as build_step
from core.graph import inspect as inspect_step
from core.pipe import parser as parser_step


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

    p_build = sub.add_parser("build", help="Ingest a parsed corpus into the Graphiti knowledge graph")
    p_build.add_argument("--target", required=True, help="Corpus name, resolved to output/<name>[-N].json")
    p_build.add_argument("--book", default=None, help="Only ingest this book (e.g. Genesis)")
    p_build.add_argument("--chapter", type=int, default=None, help="Only ingest this chapter number")
    p_build.add_argument("--limit", type=int, default=None, help="Cap the number of chapter episodes")
    p_build.add_argument("--group-id", default=None, help="Graph namespace (default: translation name)")
    p_build.add_argument("--dry-run", action="store_true", help="Show the ingestion plan without calling Neo4j/LLM")
    p_build.set_defaults(handler=lambda args: build_step.run(
        args.target,
        book=args.book,
        chapter=args.chapter,
        limit=args.limit,
        group_id=args.group_id,
        dry_run=args.dry_run,
    ))

    p_inspect = sub.add_parser("inspect", help="Show extracted entities and relationships for validation")
    p_inspect.add_argument("--group-id", default=None, help="Limit to a graph namespace (default: all)")
    p_inspect.add_argument("--limit", type=int, default=30, help="Max entities/relationships to list")
    p_inspect.set_defaults(handler=lambda args: inspect_step.run(group_id=args.group_id, limit=args.limit))

    return cli


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)
