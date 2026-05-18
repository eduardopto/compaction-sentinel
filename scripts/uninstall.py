#!/usr/bin/env python3
"""Remove Compaction Sentinel hooks and MCP config from Codex Desktop."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from compaction_sentinel.cli import main


def normalize_args(argv: list[str]) -> list[str]:
    global_args: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--codex-home" and index + 1 < len(argv):
            global_args.extend([arg, argv[index + 1]])
            index += 2
            continue
        rest.append(arg)
        index += 1
    return [*global_args, "uninstall", *rest]


if __name__ == "__main__":
    raise SystemExit(main(normalize_args(sys.argv[1:])))
