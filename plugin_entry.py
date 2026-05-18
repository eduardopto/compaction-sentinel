#!/usr/bin/env python3
"""Plugin-safe entrypoint that runs from a Codex plugin checkout/cache."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    runpy.run_module("compaction_sentinel.cli", run_name="__main__")
