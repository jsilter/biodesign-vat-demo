#!/usr/bin/env python3
"""Prepare and check the frozen BBF-14 BenchBB panel without remote calls."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from demo.benchbb_panel import BenchBBPanel, build_pinned_panel, panel_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="root containing the pinned dataset's data/ directory")
    parser.add_argument("output", type=Path, help="checked-in compact panel manifest")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify output matches the pinned source instead of writing it",
    )
    args = parser.parse_args()

    panel = build_pinned_panel(args.source)
    expected = panel_bytes(panel)
    if args.check:
        actual = args.output.read_bytes()
        if actual != expected:
            raise SystemExit("panel manifest does not match the pinned source")
        loaded = BenchBBPanel.model_validate_json(actual)
        print(f"verified {len(loaded.designs)} designs; sha256={hashlib.sha256(actual).hexdigest()}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(expected)
    print(f"wrote {len(panel.designs)} designs; sha256={hashlib.sha256(expected).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
