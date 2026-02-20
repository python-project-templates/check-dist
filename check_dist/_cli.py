"""CLI entry point for check-dist."""

from __future__ import annotations

import argparse
import sys

from ._core import CheckDistError, check_dist


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="check-dist",
        description="Check Python source and wheel distributions for correctness",
    )
    parser.add_argument(
        "source_dir",
        nargs="?",
        default=".",
        help="Source directory (default: current directory)",
    )
    parser.add_argument(
        "--no-isolation",
        action="store_true",
        help="Disable build isolation",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="List every file inside each distribution",
    )
    parser.add_argument(
        "--pre-built",
        metavar="DIR",
        default=None,
        help="Skip building and use existing dist files from DIR",
    )
    args = parser.parse_args(argv)

    try:
        success, messages = check_dist(
            source_dir=args.source_dir,
            no_isolation=args.no_isolation,
            verbose=args.verbose,
            pre_built=args.pre_built,
        )
        for msg in messages:
            print(msg)
        sys.exit(0 if success else 1)
    except CheckDistError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
