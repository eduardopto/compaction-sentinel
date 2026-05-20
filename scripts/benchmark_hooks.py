#!/usr/bin/env python3
"""Non-strict Compaction Sentinel hook benchmark."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from compaction_sentinel.core import connect, db_path, handle_hook


def timed(label: str, count: int, fn) -> dict[str, float | int | str]:
    durations: list[float] = []
    start = time.perf_counter()
    for index in range(count):
        item_start = time.perf_counter()
        fn(index)
        durations.append((time.perf_counter() - item_start) * 1000)
    total = time.perf_counter() - start
    p95 = statistics.quantiles(durations, n=20)[18] if len(durations) >= 20 else max(durations, default=0)
    return {
        "label": label,
        "count": count,
        "total_seconds": round(total, 4),
        "avg_ms": round((total / max(count, 1)) * 1000, 4),
        "p95_ms": round(p95, 4),
    }


def run_benchmark(*, quick: bool = False, performance_mode: str = "balanced") -> dict[str, object]:
    counts = {
        "pre_read": 1000,
        "post_read": 1000,
        "test_failures": 200,
        "packets": 100,
    }
    if quick:
        counts = {"pre_read": 50, "post_read": 50, "test_failures": 10, "packets": 5}

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "codex"
        project = Path(tmp) / "repo"
        project.mkdir(parents=True)
        (project / ".git").mkdir()
        config = home / "compaction-sentinel" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "performance_mode": performance_mode,
                    "max_packet_chars": 4000,
                    "retention_days": 7,
                    "retention_check_interval_seconds": 3600,
                    "prune_check_interval_events": 50,
                }
            ),
            encoding="utf-8",
        )

        results = [
            timed(
                "1000 PreToolUse read-only events" if not quick else "quick PreToolUse read-only events",
                counts["pre_read"],
                lambda index: handle_hook(
                    "PreToolUse",
                    {
                        "cwd": str(project),
                        "session_id": "bench",
                        "turn_id": "pre",
                        "tool_use_id": f"pre-{index}",
                        "tool_name": "Bash",
                        "tool_input": {"command": f"sed -n '1,120p' docs/file_{index % 10}.md"},
                    },
                    codex_home=home,
                ),
            ),
            timed(
                "1000 PostToolUse read-only results" if not quick else "quick PostToolUse read-only results",
                counts["post_read"],
                lambda index: handle_hook(
                    "PostToolUse",
                    {
                        "cwd": str(project),
                        "session_id": "bench",
                        "turn_id": "post",
                        "tool_use_id": f"post-{index}",
                        "tool_name": "Bash",
                        "tool_input": {"command": f"sed -n '1,120p' docs/file_{index % 10}.md"},
                        "tool_response": "\n".join(f"line {line}" for line in range(80)),
                    },
                    codex_home=home,
                ),
            ),
            timed(
                "200 test failures" if not quick else "quick test failures",
                counts["test_failures"],
                lambda index: handle_hook(
                    "PostToolUse",
                    {
                        "cwd": str(project),
                        "session_id": "bench",
                        "turn_id": "fail",
                        "tool_use_id": f"fail-{index}",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest tests/test_app.py"},
                        "tool_response": "FAILED tests/test_app.py::test_save - AssertionError",
                    },
                    codex_home=home,
                ),
            ),
            timed(
                "100 packet builds" if not quick else "quick packet builds",
                counts["packets"],
                lambda index: handle_hook(
                    "UserPromptSubmit",
                    {
                        "cwd": str(project),
                        "session_id": "bench",
                        "turn_id": f"packet-{index}",
                        "prompt": "Continue the benchmark task.",
                    },
                    codex_home=home,
                ),
            ),
        ]
        db = connect(home)
        try:
            event_count = db.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
        finally:
            db.close()
        ledger = db_path(home)
        return {
            "performance_mode": performance_mode,
            "quick": quick,
            "results": results,
            "db_size_bytes": ledger.stat().st_size if ledger.exists() else 0,
            "event_count": int(event_count),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="Run a small CI-friendly smoke benchmark.")
    parser.add_argument(
        "--performance-mode",
        choices=["full", "balanced", "light"],
        default="balanced",
        help="Runtime performance mode to benchmark.",
    )
    args = parser.parse_args()
    print(json.dumps(run_benchmark(quick=args.quick, performance_mode=args.performance_mode), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
