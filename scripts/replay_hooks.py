#!/usr/bin/env python3
"""Replay Codex hook scenarios against a temporary Compaction Sentinel ledger."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from compaction_sentinel.core import (  # noqa: E402
    active_checkpoint,
    build_resume_packet,
    connect,
    handle_hook,
    load_runtime_config,
    project_from_cli,
    save_checkpoint,
    scrub_project,
    write_runtime_config,
)
from compaction_sentinel.mcp_server import handle_tool_call  # noqa: E402


class ReplayFailure(AssertionError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay Compaction Sentinel hook fixtures.")
    parser.add_argument("scenarios", nargs="+", type=Path)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def replace_placeholders(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for key, replacement in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
            out = out.replace(key, replacement)
        return out
    if isinstance(value, dict):
        return {key: replace_placeholders(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_placeholders(item, mapping) for item in value]
    return value


def output_text(output: dict[str, Any]) -> str:
    if not output:
        return ""
    parts = [json.dumps(output, sort_keys=True)]
    hook_output = output.get("hookSpecificOutput")
    if isinstance(hook_output, dict):
        parts.append(str(hook_output.get("additionalContext") or ""))
    return "\n".join(parts)


def scenario_lines(path: Path) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            step = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReplayFailure(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(step, dict):
            raise ReplayFailure(f"{path}:{line_no}: step must be an object")
        steps.append(step)
    return steps


def run_scenario(path: Path, *, verbose: bool = False) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        temp = Path(tmp)
        codex_home = temp / "codex"
        project = temp / "project"
        project_a = temp / "project-a"
        project_b = temp / "project-b"
        for root in (project, project_a, project_b):
            root.mkdir(parents=True)
            (root / ".git").mkdir()
        mapping = {
            "$CODEX_HOME": str(codex_home),
            "$PROJECT": str(project),
            "$PROJECT_A": str(project_a),
            "$PROJECT_B": str(project_b),
        }
        last_output: dict[str, Any] = {}
        for index, raw_step in enumerate(scenario_lines(path), start=1):
            step = replace_placeholders(raw_step, mapping)
            if "config" in step:
                config = load_runtime_config(codex_home)
                config.update(step["config"])
                write_runtime_config(config, codex_home)
                continue
            if "checkpoint" in step:
                data = step["checkpoint"]
                cwd = data.pop("cwd", str(project))
                db = connect(codex_home)
                try:
                    save_checkpoint(db, project_from_cli(cwd), **data)
                finally:
                    db.close()
                continue
            if "event" in step:
                event_name = str(step["event"])
                payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
                last_output = handle_hook(event_name, payload, codex_home=codex_home)
                if verbose:
                    print(f"{path.name}:{index} {event_name}: {json.dumps(last_output, sort_keys=True)}")
                if "expect" in step:
                    run_assertions(
                        path,
                        index,
                        step["expect"],
                        codex_home=codex_home,
                        cwd=str(payload.get("cwd") or project),
                        last_output=last_output,
                    )
                continue
            if "repeat_event" in step:
                repeat = step["repeat_event"]
                count = int(repeat.get("count") or 0)
                event_name = str(repeat.get("event") or "")
                payload_template = repeat.get("payload") if isinstance(repeat.get("payload"), dict) else {}
                for repeat_index in range(count):
                    payload = replace_placeholders(
                        payload_template,
                        {"$INDEX": str(repeat_index), **mapping},
                    )
                    last_output = handle_hook(event_name, payload, codex_home=codex_home)
                continue
            if "mcp_call" in step:
                call = step["mcp_call"]
                req = {
                    "jsonrpc": "2.0",
                    "id": step.get("id", index),
                    "method": "tools/call",
                    "params": {
                        "name": call.get("name"),
                        "arguments": call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                    },
                }
                last_output = call_mcp(req, codex_home)
                if verbose:
                    print(f"{path.name}:{index} mcp:{call.get('name')}: {json.dumps(last_output, sort_keys=True)}")
                if "expect" in step:
                    args = req["params"]["arguments"]
                    run_assertions(
                        path,
                        index,
                        step["expect"],
                        codex_home=codex_home,
                        cwd=str(args.get("cwd") or project),
                        last_output=last_output,
                    )
                continue
            if "scrub_project" in step:
                cwd = str(step["scrub_project"].get("cwd") or project)
                db = connect(codex_home)
                try:
                    scrub_project(db, project_from_cli(cwd))
                finally:
                    db.close()
                continue
            if "assert" in step:
                assertions = step["assert"]
                cwd = str(assertions.get("cwd") or project)
                run_assertions(
                    path,
                    index,
                    assertions,
                    codex_home=codex_home,
                    cwd=cwd,
                    last_output=last_output,
                )
                continue
            raise ReplayFailure(f"{path}:{index}: unknown replay step")


def call_mcp(req: dict[str, Any], codex_home: Path) -> dict[str, Any]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        handle_tool_call(req, codex_home)
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    if not lines:
        return {}
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise ReplayFailure(f"invalid MCP output: {lines[-1]}") from exc
    return value if isinstance(value, dict) else {}


def run_assertions(
    path: Path,
    index: int,
    assertions: dict[str, Any],
    *,
    codex_home: Path,
    cwd: str,
    last_output: dict[str, Any],
) -> None:
    db = connect(codex_home)
    project = project_from_cli(cwd)
    try:
        stream_id = assertions.get("stream_id")
        packet = build_resume_packet(db, project, reason="replay", stream_id=str(stream_id) if stream_id else None)
        checkpoint = active_checkpoint(db, project, stream_id=str(stream_id) if stream_id else None)
        db_text = dump_db_text(db)
        event_count = int(
            db.execute(
                "SELECT COUNT(*) AS count FROM events WHERE project_root = ?",
                (str(project.root),),
            ).fetchone()["count"]
        )
        category_counts = {
            str(row["category"]): int(row["count"])
            for row in db.execute(
                """
                SELECT category, COUNT(*) AS count
                FROM events
                WHERE project_root = ?
                GROUP BY category
                """,
                (str(project.root),),
            ).fetchall()
        }
        state_keys = [str(row["key"]) for row in db.execute("SELECT key FROM state ORDER BY key").fetchall()]
    finally:
        db.close()
    label = f"{path}:{index}"
    for text in assertions.get("packet_contains", []):
        if text not in packet:
            raise ReplayFailure(f"{label}: packet does not contain {text!r}\n{packet}")
    for text in assertions.get("packet_not_contains", []):
        if text in packet:
            raise ReplayFailure(f"{label}: packet unexpectedly contains {text!r}\n{packet}")
    if "max_chars" in assertions and len(packet) > int(assertions["max_chars"]):
        raise ReplayFailure(f"{label}: packet length {len(packet)} exceeds {assertions['max_chars']}")
    if "active_objective_contains" in assertions:
        if checkpoint is None:
            raise ReplayFailure(f"{label}: no active checkpoint")
        expected = str(assertions["active_objective_contains"])
        if expected not in str(checkpoint["objective"]):
            raise ReplayFailure(f"{label}: active objective did not contain {expected!r}")
    if "active_next_action_contains" in assertions:
        if checkpoint is None:
            raise ReplayFailure(f"{label}: no active checkpoint")
        expected = str(assertions["active_next_action_contains"])
        if expected not in str(checkpoint["next_action"] or ""):
            raise ReplayFailure(f"{label}: active next action did not contain {expected!r}")
    text = output_text(last_output)
    for expected in assertions.get("last_output_contains", []):
        if expected not in text:
            raise ReplayFailure(f"{label}: last output did not contain {expected!r}\n{text}")
    for unexpected in assertions.get("last_output_not_contains", []):
        if unexpected in text:
            raise ReplayFailure(f"{label}: last output unexpectedly contained {unexpected!r}\n{text}")
    if assertions.get("last_output_has_decision") and "decision" not in last_output:
        raise ReplayFailure(f"{label}: last output did not include a decision")
    if assertions.get("last_output_empty") and last_output:
        raise ReplayFailure(f"{label}: expected empty output, got {last_output!r}")
    if "db_event_count" in assertions and event_count != int(assertions["db_event_count"]):
        raise ReplayFailure(f"{label}: event count {event_count} != {assertions['db_event_count']}")
    if "db_event_count_at_least" in assertions and event_count < int(assertions["db_event_count_at_least"]):
        raise ReplayFailure(f"{label}: event count {event_count} < {assertions['db_event_count_at_least']}")
    if isinstance(assertions.get("db_category_counts"), dict):
        for category, expected in assertions["db_category_counts"].items():
            actual = category_counts.get(str(category), 0)
            if actual != int(expected):
                raise ReplayFailure(
                    f"{label}: category {category!r} count {actual} != {expected}: {category_counts}"
                )
    for expected in assertions.get("db_contains", []):
        if expected not in db_text:
            raise ReplayFailure(f"{label}: DB text did not contain {expected!r}")
    for unexpected in assertions.get("db_not_contains", []):
        if unexpected in db_text:
            raise ReplayFailure(f"{label}: DB text unexpectedly contained {unexpected!r}")
    for expected in assertions.get("state_key_contains", []):
        if not any(expected in key for key in state_keys):
            raise ReplayFailure(f"{label}: no state key contained {expected!r}: {state_keys}")
    for unexpected in assertions.get("state_key_not_contains", []):
        if any(unexpected in key for key in state_keys):
            raise ReplayFailure(f"{label}: state key unexpectedly contained {unexpected!r}: {state_keys}")
    for absent in assertions.get("state_key_absent_contains", []):
        if any(absent in key for key in state_keys):
            raise ReplayFailure(f"{label}: state key still contained {absent!r}: {state_keys}")
    if "state_key_count" in assertions and len(state_keys) != int(assertions["state_key_count"]):
        raise ReplayFailure(f"{label}: state key count {len(state_keys)} != {assertions['state_key_count']}: {state_keys}")
    active_fields = assertions.get("active_checkpoint_fields")
    if isinstance(active_fields, dict):
        if checkpoint is None:
            raise ReplayFailure(f"{label}: no active checkpoint")
        for field, expected in active_fields.items():
            if str(expected) not in str(checkpoint[field] or ""):
                raise ReplayFailure(f"{label}: active checkpoint {field} did not contain {expected!r}")


def dump_db_text(db: Any) -> str:
    chunks: list[str] = []
    for table in ("events", "notes", "checkpoints", "state"):
        for row in db.execute(f"SELECT * FROM {table}").fetchall():
            chunks.append(json.dumps(dict(row), sort_keys=True, default=str))
    return "\n".join(chunks)


def main() -> int:
    args = parse_args()
    failures: list[str] = []
    for scenario in args.scenarios:
        try:
            run_scenario(scenario, verbose=args.verbose)
            print(f"PASS {scenario}")
        except Exception as exc:
            failures.append(f"FAIL {scenario}: {exc}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
