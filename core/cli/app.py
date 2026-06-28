"""CLI wiring — registers subcommands and dispatches to pipeline handlers."""
from __future__ import annotations

import argparse

from core.cost import estimate as estimate_step
from core.graph import build as build_step
from core.graph import inspect as inspect_step
from core.graph import transfer as transfer_step
from core.pipe import parser as parser_step
from core.pipe import pericope as pericope_step


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
    p_peri.add_argument("--chapter", type=int, default=None, help="Only segment this chapter number")
    p_peri.add_argument("--limit", type=int, default=None, help="Cap the number of chapters to segment")
    p_peri.add_argument("--model", default=None, help="Override the LLM model id (default: LLM_MODEL from .env)")
    p_peri.add_argument("--force", action="store_true", help="Re-segment chapters already in the cache")
    p_peri.add_argument("--dry-run", action="store_true", help="Show the plan without calling the LLM")
    p_peri.set_defaults(handler=lambda args: pericope_step.run(
        args.target,
        book=args.book,
        chapter=args.chapter,
        limit=args.limit,
        model=args.model,
        force=args.force,
        dry_run=args.dry_run,
    ))

    p_build = sub.add_parser("build", help="Ingest a parsed corpus into the Graphiti knowledge graph")
    p_build.add_argument("--target", required=True, help="Corpus name, resolved to output/<name>[-N].json")
    p_build.add_argument("--book", default=None, help="Only ingest this book (e.g. Genesis)")
    p_build.add_argument("--chapter", type=int, default=None, help="Only ingest this chapter number")
    p_build.add_argument("--limit", type=int, default=None, help="Cap the number of episodes")
    p_build.add_argument("--group-id", default=None, help="Graph namespace (default: translation name)")
    p_build.add_argument("--no-pericope", dest="pericope", action="store_false", default=True,
                         help="Ingest one episode per chapter (default: per pericope, LLM-segmented and cached on demand)")
    p_build.add_argument("--no-enrich", dest="enrich", action="store_false", default=True,
                         help="Skip per-episode LLM extraction-guidance enrichment (default: on, cached)")
    p_build.add_argument("--dry-run", action="store_true", help="Show the ingestion plan without calling Neo4j/LLM")
    resume_grp = p_build.add_mutually_exclusive_group()
    resume_grp.add_argument("--resume", dest="resume", action="store_true", default=None,
                            help="Skip episodes already in the group, no prompt (resume an interrupted run)")
    resume_grp.add_argument("--no-resume", dest="resume", action="store_false", default=None,
                            help="Re-ingest every selected episode, ignoring existing nodes (no prompt)")
    p_build.set_defaults(handler=lambda args: build_step.run(
        args.target,
        book=args.book,
        chapter=args.chapter,
        limit=args.limit,
        group_id=args.group_id,
        dry_run=args.dry_run,
        pericope=args.pericope,
        resume=args.resume,
        enrich=args.enrich,
    ))

    p_transfer = sub.add_parser("transfer", help="Export/import the Neo4j graph as JSON (backup/restore)")
    transfer_sub = p_transfer.add_subparsers(dest="transfer_command", required=True)

    p_export = transfer_sub.add_parser("export", help="Dump the graph to export/<name>-<N>.json")
    p_export.add_argument("--target", required=True, help="Output name, resolved to export/<name>-<N>.json")
    p_export.add_argument("--group-id", default=None, help="Only export this graph namespace (default: whole graph)")
    p_export.add_argument("--no-embeddings", action="store_true",
                          help="Strip *_embedding vectors (smaller file; search needs re-embedding after import)")
    p_export.add_argument("--dry-run", action="store_true", help="Show what would be exported without writing a file")
    p_export.set_defaults(handler=lambda args: transfer_step.export_run(
        args.target,
        group_id=args.group_id,
        embeddings=not args.no_embeddings,
        dry_run=args.dry_run,
    ))

    p_import = transfer_sub.add_parser("import", help="Load export/<name>[-N].json into Neo4j (MERGE by uuid)")
    p_import.add_argument("--target", required=True, help="Input name, resolved to export/<name>[-N].json")
    p_import.add_argument("--wipe", action="store_true",
                          help="DETACH DELETE the file's group_id (or whole graph) before loading")
    p_import.add_argument("--dry-run", action="store_true", help="Show the import plan without touching Neo4j")
    p_import.set_defaults(handler=lambda args: transfer_step.import_run(
        args.target,
        wipe=args.wipe,
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
