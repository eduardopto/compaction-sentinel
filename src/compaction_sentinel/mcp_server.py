"""Small dependency-free MCP stdio server for Compaction Sentinel."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .core import (
    VERSION,
    active_checkpoint,
    append_checkpoint_field,
    build_resume_packet,
    connect,
    project_from_cli,
    recent_events,
    save_checkpoint,
    save_note,
    search_events,
)


CHECKPOINT_PROPERTIES: dict[str, Any] = {
    "objective": {"type": "string"},
    "status": {"type": "string", "enum": ["active", "blocked", "complete"], "default": "active"},
    "acceptance_criteria": {"type": "string"},
    "current_step": {"type": "string"},
    "next_action": {"type": "string"},
    "blockers": {"type": "string"},
    "evidence": {"type": "string"},
    "files_touched": {"type": "string"},
    "commands_run": {"type": "string"},
    "tests_passed": {"type": "string"},
    "tests_failed": {"type": "string"},
    "decisions_made": {"type": "string"},
    "assumptions": {"type": "string"},
    "do_not_repeat": {"type": "string"},
    "last_verified_at": {"type": "string"},
    "confidence": {"type": "string"},
    "cwd": {"type": "string"},
}


TOOLS = [
    {
        "name": "compaction_checkpoint",
        "description": "Save a durable checkpoint with objective, acceptance criteria, state, next action, evidence, decisions, and do-not-repeat guidance.",
        "inputSchema": {
            "type": "object",
            "properties": CHECKPOINT_PROPERTIES,
            "required": ["objective"],
        },
    },
    {
        "name": "compaction_evidence_add",
        "description": "Append evidence, command output, tests, or touched files to the active checkpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["evidence", "commands_run", "tests_passed", "tests_failed", "files_touched"],
                    "default": "evidence",
                },
                "objective": {"type": "string"},
                "cwd": {"type": "string"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "compaction_avoid_add",
        "description": "Append a do-not-repeat item to the active checkpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "objective": {"type": "string"},
                "cwd": {"type": "string"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "compaction_note",
        "description": "Save a compact continuity note that should survive compaction.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "surface_condition": {"type": "string"},
                "cwd": {"type": "string"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "compaction_packet",
        "description": "Return the current packet-v2 resume brief for the active project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "max_chars": {"type": "integer", "default": 9000},
            },
        },
    },
    {
        "name": "compaction_search",
        "description": "Search recent Compaction Sentinel events for the active project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "cwd": {"type": "string"},
                "limit": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "compaction_status",
        "description": "Show active checkpoint and recent event status for the active project.",
        "inputSchema": {
            "type": "object",
            "properties": {"cwd": {"type": "string"}},
        },
    },
]


def send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(req_id: Any, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}]}}


def error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_initialize(req: dict[str, Any]) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "id": req.get("id"),
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "compaction-sentinel", "version": VERSION},
            },
        }
    )


def handle_tool_call(req: dict[str, Any], codex_home: Path | None) -> None:
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    name = params.get("name")
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    db = connect(codex_home)
    project = project_from_cli(args.get("cwd") if isinstance(args.get("cwd"), str) else None)
    try:
        if name == "compaction_checkpoint":
            checkpoint_id = save_checkpoint(
                db,
                project,
                objective=str(args.get("objective") or ""),
                status=str(args.get("status") or "active"),
                acceptance_criteria=str(args.get("acceptance_criteria") or ""),
                current_step=str(args.get("current_step") or ""),
                next_action=str(args.get("next_action") or ""),
                blockers=str(args.get("blockers") or ""),
                evidence=str(args.get("evidence") or ""),
                files_touched=str(args.get("files_touched") or ""),
                commands_run=str(args.get("commands_run") or ""),
                tests_passed=str(args.get("tests_passed") or ""),
                tests_failed=str(args.get("tests_failed") or ""),
                decisions_made=str(args.get("decisions_made") or ""),
                assumptions=str(args.get("assumptions") or ""),
                do_not_repeat=str(args.get("do_not_repeat") or ""),
                last_verified_at=str(args.get("last_verified_at") or ""),
                confidence=str(args.get("confidence") or ""),
                source="mcp",
            )
            send(result(req.get("id"), f"Saved Compaction Sentinel checkpoint #{checkpoint_id} for {project.name}."))
            return
        if name == "compaction_evidence_add":
            checkpoint_id = append_checkpoint_field(
                db,
                project,
                field=str(args.get("kind") or "evidence"),
                value=str(args.get("content") or ""),
                objective=str(args.get("objective") or "") or None,
            )
            send(result(req.get("id"), f"Updated Compaction Sentinel checkpoint #{checkpoint_id} for {project.name}."))
            return
        if name == "compaction_avoid_add":
            checkpoint_id = append_checkpoint_field(
                db,
                project,
                field="do_not_repeat",
                value=str(args.get("content") or ""),
                objective=str(args.get("objective") or "") or None,
            )
            send(result(req.get("id"), f"Updated Compaction Sentinel checkpoint #{checkpoint_id} for {project.name}."))
            return
        if name == "compaction_note":
            note_id = save_note(
                db,
                project,
                str(args.get("content") or ""),
                surface_condition=str(args.get("surface_condition") or ""),
            )
            send(result(req.get("id"), f"Saved Compaction Sentinel note #{note_id} for {project.name}."))
            return
        if name == "compaction_packet":
            send(
                result(
                    req.get("id"),
                    build_resume_packet(
                        db,
                        project,
                        reason="mcp",
                        max_chars=int(args.get("max_chars") or 9000),
                    ),
                )
            )
            return
        if name == "compaction_search":
            rows = search_events(db, project, str(args.get("query") or ""), limit=int(args.get("limit") or 8))
            text = "\n".join(
                f"[{row['id']}] {row['created_at']} {row['event_name']}/{row['kind']}: {row['summary']}"
                for row in rows
            )
            send(result(req.get("id"), text or "No matching Compaction Sentinel events."))
            return
        if name == "compaction_status":
            checkpoint = active_checkpoint(db, project)
            events = recent_events(db, project, limit=6)
            send(
                result(
                    req.get("id"),
                    json.dumps(
                        {
                            "project": project.name,
                            "project_root": str(project.root),
                            "active_checkpoint": dict(checkpoint) if checkpoint else None,
                            "recent_events": [dict(row) for row in events],
                        },
                        indent=2,
                    ),
                )
            )
            return
        send(error(req.get("id"), -32602, f"Unknown tool: {name}"))
    except Exception as exc:
        send(error(req.get("id"), -32000, str(exc)))
    finally:
        db.close()


def loop(codex_home: Path | None = None) -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if not isinstance(req, dict):
            continue
        method = req.get("method")
        if method == "initialize":
            handle_initialize(req)
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req.get("id"), "result": {"tools": TOOLS}})
        elif method == "tools/call":
            handle_tool_call(req, codex_home)
        elif "id" in req:
            send(error(req.get("id"), -32601, f"Unsupported method: {method}"))
    return 0
