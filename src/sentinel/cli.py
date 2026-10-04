"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sentinel import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel", description="Sentinel FX command line")
    parser.add_argument("--version", action="version", version=f"sentinel {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help(sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
