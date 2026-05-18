"""Command-line entrypoint for Compaction Sentinel."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import install as installer
from .core import (
    VERSION,
    active_checkpoint,
    build_resume_packet,
    connect,
    default_codex_home,
    handle_hook,
    log,
    project_from_cli,
    read_json_stdin,
    recent_events,
    save_checkpoint,
    save_note,
    search_events,
    safe_print_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compaction-sentinel",
        description="Codex Desktop continuity hooks, skill, and MCP tools.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--codex-home", type=Path, default=None, help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex.")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="Install hooks, skill, CLI shims, and MCP config into Codex Desktop.")
    install.add_argument("--enable-stop-continue", action="store_true", help="Shortcut for --auto-continue gentle.")
    install.add_argument(
        "--auto-continue",
        choices=["off", "gentle", "strict"],
        default=None,
        help="Stop-hook continuation policy. Defaults to preserving the current setting, or off on first install.",
    )
    install.add_argument("--skills-target", choices=["codex", "agents", "both"], default="both", help="Where to copy the skill.")
    install.add_argument("--dry-run", action="store_true", help="Show what would change without writing files.")

    sub.add_parser("uninstall", help="Remove installed Compaction Sentinel hooks and MCP config.")
    sub.add_parser("doctor", help="Print install and runtime status.")

    hook = sub.add_parser("hook", help="Run a Codex hook handler. Used by hooks.json.")
    hook.add_argument("event_name", help="Hook event name such as UserPromptSubmit, SessionStart, PreToolUse, PostToolUse, or Stop.")

    checkpoint = sub.add_parser("checkpoint", help="Write a manual continuity checkpoint for the current project.")
    checkpoint.add_argument("--objective", required=True)
    checkpoint.add_argument("--status", choices=["active", "blocked", "complete"], default="active")
    checkpoint.add_argument("--current-step", default="")
    checkpoint.add_argument("--next-action", default="")
    checkpoint.add_argument("--blockers", default="")
    checkpoint.add_argument("--evidence", default="")
    checkpoint.add_argument("--cwd", default=None)

    note = sub.add_parser("note", help="Write a continuity note for the current project.")
    note.add_argument("content")
    note.add_argument("--when", dest="surface_condition", default="")
    note.add_argument("--cwd", default=None)

    packet = sub.add_parser("packet", help="Print the current resume packet for the current project.")
    packet.add_argument("--cwd", default=None)
    packet.add_argument("--max-chars", type=int, default=9000)

    status = sub.add_parser("status", help="Show runtime status for the current project.")
    status.add_argument("--cwd", default=None)

    search = sub.add_parser("search", help="Search recorded events for the current project.")
    search.add_argument("query")
    search.add_argument("--cwd", default=None)
    search.add_argument("--limit", type=int, default=8)

    sub.add_parser("mcp", help="Run the MCP stdio server.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    codex_home = args.codex_home or default_codex_home()

    if args.command == "install":
        result = installer.install(
            source_root=Path(__file__).resolve().parents[2],
            codex_home=codex_home,
            enable_stop_continue=args.enable_stop_continue,
            auto_continue=args.auto_continue,
            skills_target=args.skills_target,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "uninstall":
        result = installer.uninstall(codex_home=codex_home)
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "doctor":
        result = installer.doctor(codex_home=codex_home)
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "hook":
        payload = read_json_stdin()
        try:
            safe_print_json(handle_hook(args.event_name, payload, codex_home=codex_home))
        except Exception as exc:
            log(f"hook {args.event_name} failed open: {exc}", codex_home)
            safe_print_json({})
        return 0

    if args.command == "checkpoint":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        try:
            checkpoint_id = save_checkpoint(
                db,
                project,
                objective=args.objective,
                status=args.status,
                current_step=args.current_step,
                next_action=args.next_action,
                blockers=args.blockers,
                evidence=args.evidence,
                source="cli",
            )
        finally:
            db.close()
        print(f"Saved checkpoint #{checkpoint_id} for {project.name}.")
        return 0

    if args.command == "note":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        try:
            note_id = save_note(db, project, args.content, surface_condition=args.surface_condition)
        finally:
            db.close()
        print(f"Saved note #{note_id} for {project.name}.")
        return 0

    if args.command == "packet":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        try:
            print(build_resume_packet(db, project, reason="cli", max_chars=args.max_chars))
        finally:
            db.close()
        return 0

    if args.command == "status":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        try:
            checkpoint = active_checkpoint(db, project)
            events = recent_events(db, project, limit=5)
        finally:
            db.close()
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "project": project.name,
                    "project_root": str(project.root),
                    "codex_home": str(codex_home),
                    "active_checkpoint": dict(checkpoint) if checkpoint else None,
                    "recent_events": [dict(row) for row in events],
                },
                indent=2,
            )
        )
        return 0

    if args.command == "search":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        try:
            rows = search_events(db, project, args.query, limit=args.limit)
        finally:
            db.close()
        for row in rows:
            print(f"[{row['id']}] {row['created_at']} {row['event_name']}/{row['kind']}: {row['summary']}")
        return 0

    if args.command == "mcp":
        from .mcp_server import loop

        return loop(codex_home=codex_home)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
