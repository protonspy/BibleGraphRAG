"""CLI wiring — registers subcommands and dispatches to pipeline handlers."""
from __future__ import annotations

import argparse

from core.pipe import parser as parser_step


def build_parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="bgr", description="BibleGraphRAG CLI")
    sub = cli.add_subparsers(dest="command", required=True)

    p_parser = sub.add_parser("parser", help="Normalize raw text into structured JSON")
    p_parser.add_argument("--target", required=True, help="Input name, resolved to data/<name>.txt")
    p_parser.set_defaults(handler=lambda args: parser_step.run(args.target))

    return cli


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)
