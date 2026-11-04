"""Command-line entry point. Subcommands (run, setup-arr, ...) arrive in later milestones."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from smarrtbot import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smarrtbot", description=__doc__)
    parser.add_argument("--version", action="version", version=f"smarrtbot {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print(f"smarrtbot {__version__}: nothing to run yet (M0 skeleton)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
