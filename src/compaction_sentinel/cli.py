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
    DEFAULT_MAX_PACKET_CHARS,
    PERFORMANCE_MODES,
    VERSION,
    active_checkpoint,
    append_checkpoint_field,
    backup_codex_context_state,
    build_resume_packet,
    compact_audit,
    compact_status,
    config_int,
    connect,
    default_codex_home,
    export_project,
    handle_hook,
    import_codex_context,
    inspect_codex_context,
    latest_checkpoint,
    list_memory_candidates,
    list_quarantine,
    load_runtime_config,
    log,
    list_streams,
    peer_conflict_warnings,
    project_from_cli,
    read_json_stdin,
    recent_events,
    remember_stream,
    save_checkpoint,
    save_note,
    scrub_all,
    scrub_project,
    search_events,
    safe_print_json,
    stream_from_cli_args,
    set_quarantine,
    write_migration_rollback,
    write_runtime_config,
)


def add_stream_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stream", default=None, help="Optional Sentinel stream id/work lane.")
    parser.add_argument("--stream-label", default="", help="Optional human-readable stream label.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compaction-sentinel",
        description="Codex Desktop continuity hooks, skill, and MCP tools.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=None,
        help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex.",
    )
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
    install.add_argument(
        "--hooks-profile",
        choices=["full", "balanced", "light"],
        default="balanced",
        help="Hook install profile. light omits PreToolUse/PostToolUse hot hooks.",
    )
    install.add_argument(
        "--global-bin",
        type=Path,
        default=None,
        help="Opt in to global cs/compaction-sentinel shims in this bin directory, such as /opt/homebrew/bin.",
    )
    install.add_argument("--dry-run", action="store_true", help="Show what would change without writing files.")
    install.add_argument("--doctor", action="store_true", help="Run doctor after installation.")

    uninstall = sub.add_parser("uninstall", help="Remove installed Compaction Sentinel hooks and MCP config.")
    uninstall.add_argument("--purge", action="store_true", help="Also remove the local runtime and ledger.")

    doctor = sub.add_parser("doctor", help="Print install and runtime status.")
    doctor.add_argument("--fix", action="store_true", help="Repair missing hooks, MCP config, hook feature, skill copy, and CLI links.")
    doctor.add_argument(
        "--global-bin",
        type=Path,
        default=None,
        help="With --fix, opt in to global cs/compaction-sentinel shims in this bin directory.",
    )
    doctor.add_argument("--explain", action="store_true", help="Include human-readable issue explanations.")

    hook = sub.add_parser("hook", help="Run a Codex hook handler. Used by hooks.json.")
    hook.add_argument(
        "event_name",
        help="Hook event name such as UserPromptSubmit, SessionStart, PreToolUse, PermissionRequest, PostToolUse, or Stop.",
    )

    checkpoint = sub.add_parser("checkpoint", help="Write a manual continuity checkpoint for the current project.")
    checkpoint.add_argument("--objective", default="")
    checkpoint.add_argument("--same-objective", action="store_true", help="Reuse the active/latest checkpoint objective.")
    checkpoint.add_argument("--status", choices=["active", "blocked", "complete"], default="active")
    checkpoint.add_argument("--acceptance-criteria", default="")
    checkpoint.add_argument("--current-step", default="")
    checkpoint.add_argument("--next-action", default="")
    checkpoint.add_argument("--blockers", default="")
    checkpoint.add_argument("--evidence", default="")
    checkpoint.add_argument("--files", dest="files_touched", default="")
    checkpoint.add_argument("--commands", dest="commands_run", default="")
    checkpoint.add_argument("--tests-passed", default="")
    checkpoint.add_argument("--tests-failed", default="")
    checkpoint.add_argument("--decisions", dest="decisions_made", default="")
    checkpoint.add_argument("--assumptions", default="")
    checkpoint.add_argument("--avoid", dest="do_not_repeat", default="")
    checkpoint.add_argument("--last-verified-at", default="")
    checkpoint.add_argument("--confidence", default="")
    checkpoint.add_argument("--cwd", default=None)
    add_stream_args(checkpoint)

    note = sub.add_parser("note", help="Write a continuity note for the current project.")
    note.add_argument("content")
    note.add_argument("--when", dest="surface_condition", default="")
    note.add_argument("--cwd", default=None)
    add_stream_args(note)

    evidence = sub.add_parser("evidence", help="Append evidence to the active checkpoint.")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_add = evidence_sub.add_parser("add", help="Append verification evidence.")
    evidence_add.add_argument("content")
    evidence_add.add_argument(
        "--kind",
        choices=["evidence", "commands_run", "tests_passed", "tests_failed", "files_touched"],
        default="evidence",
    )
    evidence_add.add_argument("--objective", default="")
    evidence_add.add_argument("--cwd", default=None)
    add_stream_args(evidence_add)

    avoid = sub.add_parser("avoid", help="Append a do-not-repeat item to the active checkpoint.")
    avoid_sub = avoid.add_subparsers(dest="avoid_command", required=True)
    avoid_add = avoid_sub.add_parser("add", help="Record a command, route, or hypothesis not to repeat.")
    avoid_add.add_argument("content")
    avoid_add.add_argument("--objective", default="")
    avoid_add.add_argument("--cwd", default=None)
    add_stream_args(avoid_add)

    packet = sub.add_parser("packet", help="Print the current resume packet for the current project.")
    packet.add_argument("--cwd", default=None)
    packet.add_argument("--max-chars", type=int, default=None)
    add_stream_args(packet)

    status = sub.add_parser("status", help="Show runtime status for the current project.")
    status.add_argument("--cwd", default=None)
    add_stream_args(status)

    search = sub.add_parser("search", help="Search recorded events for the current project.")
    search.add_argument("query")
    search.add_argument("--cwd", default=None)
    search.add_argument("--limit", type=int, default=8)
    add_stream_args(search)

    scrub = sub.add_parser("scrub", help="Delete stored Sentinel data.")
    scrub_scope = scrub.add_mutually_exclusive_group(required=True)
    scrub_scope.add_argument("--project", action="store_true", help="Delete data for the current project only.")
    scrub_scope.add_argument("--all", action="store_true", help="Delete all Sentinel ledger data.")
    scrub.add_argument("--cwd", default=None)
    scrub.add_argument("--stream", default=None, help="Delete only one stream within the current project.")
    scrub.add_argument("--stream-label", default="", help="Accepted for command symmetry; scrub uses --stream.")

    export = sub.add_parser("export", help="Export Sentinel data as JSON.")
    export.add_argument("--project", action="store_true", help="Export the current project.")
    export.add_argument("--cwd", default=None)
    export.add_argument("--output", type=Path, default=None)
    export.add_argument("--stream", default=None, help="Export only one stream within the current project.")
    export.add_argument("--stream-label", default="", help="Accepted for command symmetry; export uses --stream.")

    stream = sub.add_parser("stream", help="Manage Sentinel workstreams for this project.")
    stream_sub = stream.add_subparsers(dest="stream_command", required=True)
    stream_list = stream_sub.add_parser("list", help="List active/recent streams.")
    stream_list.add_argument("--cwd", default=None)
    stream_status = stream_sub.add_parser("status", help="Show current stream status.")
    stream_status.add_argument("--cwd", default=None)
    add_stream_args(stream_status)
    stream_claim = stream_sub.add_parser("claim", help="Claim or label the current stream.")
    stream_claim.add_argument("--cwd", default=None)
    add_stream_args(stream_claim)
    stream_claim.add_argument("--label", dest="stream_label", default="", help="Alias for --stream-label.")
    stream_rename = stream_sub.add_parser("rename", help="Rename a stream label.")
    stream_rename.add_argument("--cwd", default=None)
    stream_rename.add_argument("--stream", required=True)
    stream_rename.add_argument("--label", dest="stream_label", required=True)

    compact = sub.add_parser("compact", help="Inspect compact-event capture status.")
    compact_sub = compact.add_subparsers(dest="compact_command", required=True)
    compact_status_parser = compact_sub.add_parser("status", help="Show compact epoch and smoke-gate status.")
    compact_status_parser.add_argument("--cwd", default=None)
    add_stream_args(compact_status_parser)
    compact_audit_parser = compact_sub.add_parser("audit", help="Show recent PreCompact/PostCompact capture events.")
    compact_audit_parser.add_argument("--cwd", default=None)
    compact_audit_parser.add_argument("--limit", type=int, default=20)
    add_stream_args(compact_audit_parser)

    quarantine = sub.add_parser("quarantine", help="Inspect or change quarantined imported/foreign rows.")
    quarantine_sub = quarantine.add_subparsers(dest="quarantine_command", required=True)
    quarantine_list = quarantine_sub.add_parser("list", help="List quarantined rows.")
    quarantine_list.add_argument("--cwd", default=None)
    quarantine_list.add_argument("--limit", type=int, default=50)
    quarantine_claim = quarantine_sub.add_parser("claim", help="Claim a quarantined row for the current project.")
    quarantine_claim.add_argument("table", choices=["events", "checkpoints", "notes", "memory_candidates"])
    quarantine_claim.add_argument("row_id", type=int)
    quarantine_claim.add_argument("--cwd", default=None)
    quarantine_release = quarantine_sub.add_parser("release", help="Release a row back into quarantine.")
    quarantine_release.add_argument("table", choices=["events", "checkpoints", "notes", "memory_candidates"])
    quarantine_release.add_argument("row_id", type=int)
    quarantine_release.add_argument("--reason", default="manual_quarantine")
    quarantine_release.add_argument("--foreign-project-hint", default="")
    quarantine_release.add_argument("--cwd", default=None)

    memory_candidates = sub.add_parser("memory-candidates", help="List imported memory candidates.")
    memory_candidates.add_argument("--cwd", default=None)
    memory_candidates.add_argument("--include-quarantined", action="store_true")
    memory_candidates.add_argument("--limit", type=int, default=50)

    migrate = sub.add_parser("migrate", help="Run staged migrations from adjacent continuity tools.")
    migrate_sub = migrate.add_subparsers(dest="migrate_command", required=True)
    migrate_cc = migrate_sub.add_parser("codex-context", help="Inspect or import ~/.codex/codex-context state.")
    migrate_cc_mode = migrate_cc.add_mutually_exclusive_group(required=True)
    migrate_cc_mode.add_argument("--dry-run", action="store_true", help="Inspect codex-context and print planned actions only.")
    migrate_cc_mode.add_argument("--apply", action="store_true", help="Back up state, import rows, and replace codex-context hooks.")

    retention = sub.add_parser("retention", help="Manage data retention.")
    retention_sub = retention.add_subparsers(dest="retention_command", required=True)
    retention_set = retention_sub.add_parser("set", help="Set retention days.")
    retention_set.add_argument("--days", type=int, required=True)

    config = sub.add_parser("config", help="Show or change runtime config.")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="Show runtime config.")
    config_set = config_sub.add_parser("set", help="Set a runtime config key.")
    config_set.add_argument("key")
    config_set.add_argument("value")

    backup = sub.add_parser("backup", help="List or restore installer backups.")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    backup_sub.add_parser("list", help="List backups.")
    backup_restore = backup_sub.add_parser("restore", help="Restore one backup by id/name.")
    backup_restore.add_argument("backup_id")

    sub.add_parser("mcp", help="Run the MCP stdio server.")
    return parser


def parse_config_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    try:
        return int(value)
    except ValueError:
        return value


def validate_config_update(key: str, value: Any) -> Any:
    if key == "performance_mode":
        mode = str(value).strip().lower()
        if mode not in PERFORMANCE_MODES:
            raise SystemExit("performance_mode must be one of: full, balanced, light")
        return mode
    if key == "hooks_profile":
        profile = str(value).strip().lower()
        if profile not in {"full", "balanced", "light"}:
            raise SystemExit("hooks_profile must be one of: full, balanced, light")
        return profile
    return value


def json_print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def checkpoint_objective(args: argparse.Namespace, db: Any, project: Any, stream_id: str) -> str:
    if args.objective:
        return str(args.objective)
    if args.same_objective:
        checkpoint = active_checkpoint(db, project, stream_id=stream_id) or latest_checkpoint(
            db,
            project,
            stream_id=stream_id,
        )
        if checkpoint:
            return str(checkpoint["objective"])
    raise SystemExit("--objective is required unless --same-objective can reuse an existing checkpoint")


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
            hooks_profile=args.hooks_profile,
            global_bin=args.global_bin,
            dry_run=args.dry_run,
        )
        if args.doctor and not args.dry_run:
            result["doctor"] = installer.doctor(codex_home=codex_home)
        json_print(result)
        return 0

    if args.command == "uninstall":
        result = installer.uninstall(codex_home=codex_home, purge=args.purge)
        json_print(result)
        return 0

    if args.command == "doctor":
        if args.fix:
            result = installer.doctor_fix(codex_home=codex_home, global_bin=args.global_bin)
        else:
            result = installer.doctor(codex_home=codex_home)
        if args.explain:
            result["explanations"] = installer.doctor_explanations(result)
        json_print(result)
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
        config = load_runtime_config(codex_home)
        try:
            stream = stream_from_cli_args(args, project, config, db)
            objective = checkpoint_objective(args, db, project, stream.id)
            checkpoint_id = save_checkpoint(
                db,
                project,
                objective=objective,
                status=args.status,
                acceptance_criteria=args.acceptance_criteria,
                current_step=args.current_step,
                next_action=args.next_action,
                blockers=args.blockers,
                evidence=args.evidence,
                files_touched=args.files_touched,
                commands_run=args.commands_run,
                tests_passed=args.tests_passed,
                tests_failed=args.tests_failed,
                decisions_made=args.decisions_made,
                assumptions=args.assumptions,
                do_not_repeat=args.do_not_repeat,
                last_verified_at=args.last_verified_at,
                confidence=args.confidence,
                source="cli",
                redact=bool(config.get("redact", True)),
                stream=stream,
            )
            checkpoint = active_checkpoint(db, project, stream_id=stream.id)
            conflicts = peer_conflict_warnings(db, project, stream.id, checkpoint=checkpoint)
        finally:
            db.close()
        print(f"Saved checkpoint #{checkpoint_id} for {project.name} stream {stream.id}.")
        for conflict in conflicts:
            print(f"Peer awareness: {conflict}")
        return 0

    if args.command == "note":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        config = load_runtime_config(codex_home)
        try:
            stream = stream_from_cli_args(args, project, config, db)
            note_id = save_note(
                db,
                project,
                args.content,
                surface_condition=args.surface_condition,
                redact=bool(config.get("redact", True)),
                stream=stream,
            )
        finally:
            db.close()
        print(f"Saved note #{note_id} for {project.name} stream {stream.id}.")
        return 0

    if args.command == "evidence":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        config = load_runtime_config(codex_home)
        try:
            stream = stream_from_cli_args(args, project, config, db)
            checkpoint_id = append_checkpoint_field(
                db,
                project,
                field=args.kind,
                value=args.content,
                objective=args.objective or None,
                redact=bool(config.get("redact", True)),
                stream=stream,
            )
        finally:
            db.close()
        print(f"Updated checkpoint #{checkpoint_id} for {project.name} stream {stream.id}.")
        return 0

    if args.command == "avoid":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        config = load_runtime_config(codex_home)
        try:
            stream = stream_from_cli_args(args, project, config, db)
            checkpoint_id = append_checkpoint_field(
                db,
                project,
                field="do_not_repeat",
                value=args.content,
                objective=args.objective or None,
                redact=bool(config.get("redact", True)),
                stream=stream,
            )
        finally:
            db.close()
        print(f"Updated checkpoint #{checkpoint_id} for {project.name} stream {stream.id}.")
        return 0

    if args.command == "packet":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        config = load_runtime_config(codex_home)
        max_chars = args.max_chars if args.max_chars is not None else config_int(
            config,
            "max_packet_chars",
            DEFAULT_MAX_PACKET_CHARS,
            minimum=500,
        )
        try:
            stream = stream_from_cli_args(args, project, config, db)
            print(build_resume_packet(db, project, reason="cli", max_chars=max_chars, stream=stream))
        finally:
            db.close()
        return 0

    if args.command == "status":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        config = load_runtime_config(codex_home)
        try:
            stream = stream_from_cli_args(args, project, config, db)
            checkpoint = active_checkpoint(db, project, stream_id=stream.id)
            events = recent_events(db, project, limit=5, stream_id=stream.id)
            streams = list_streams(db, project)
            conflicts = peer_conflict_warnings(db, project, stream.id, checkpoint=checkpoint)
        finally:
            db.close()
        json_print(
            {
                "version": VERSION,
                "project": project.name,
                "project_root": str(project.root),
                "codex_home": str(codex_home),
                "current_stream": {"stream_id": stream.id, "stream_label": stream.label},
                "active_checkpoint": dict(checkpoint) if checkpoint else None,
                "peer_streams": [item for item in streams if item.get("stream_id") != stream.id],
                "peer_conflicts": conflicts,
                "recent_events": [dict(row) for row in events],
            }
        )
        return 0

    if args.command == "search":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        config = load_runtime_config(codex_home)
        try:
            stream = stream_from_cli_args(args, project, config, db)
            rows = search_events(db, project, args.query, limit=args.limit, stream_id=stream.id)
        finally:
            db.close()
        for row in rows:
            print(f"[{row['id']}] {row['created_at']} {row['event_name']}/{row['kind']}: {row['summary']}")
        return 0

    if args.command == "scrub":
        db = connect(codex_home)
        try:
            if args.all:
                result = {"scope": "all", "deleted": scrub_all(db)}
            else:
                project = project_from_cli(args.cwd)
                result = {
                    "scope": "project",
                    "project": project.name,
                    "project_root": str(project.root),
                    "stream_id": args.stream,
                    "deleted": scrub_project(db, project, stream_id=args.stream),
                }
        finally:
            db.close()
        json_print(result)
        return 0

    if args.command == "export":
        db = connect(codex_home)
        project = project_from_cli(args.cwd)
        try:
            data = export_project(db, project, stream_id=args.stream)
        finally:
            db.close()
        text = json.dumps(data, indent=2, sort_keys=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n", encoding="utf-8")
            print(f"Exported {project.name} to {args.output}.")
        else:
            print(text)
        return 0

    if args.command == "stream":
        db = connect(codex_home)
        project = project_from_cli(getattr(args, "cwd", None))
        config = load_runtime_config(codex_home)
        try:
            if args.stream_command == "list":
                streams = list_streams(db, project)
                conflicts_by_stream = {
                    item["stream_id"]: peer_conflict_warnings(db, project, str(item["stream_id"]))
                    for item in streams
                    if item.get("status") in {"active", "blocked"}
                }
                json_print(
                    {
                        "project": project.name,
                        "project_root": str(project.root),
                        "streams": streams,
                        "peer_conflicts_by_stream": {
                            stream_id: conflicts
                            for stream_id, conflicts in conflicts_by_stream.items()
                            if conflicts
                        },
                    }
                )
                return 0
            stream = stream_from_cli_args(args, project, config, db)
            if args.stream_command in {"claim", "rename"}:
                remember_stream(db, project, stream)
            checkpoint = active_checkpoint(db, project, stream_id=stream.id)
            events = recent_events(db, project, limit=5, stream_id=stream.id)
            streams = list_streams(db, project)
            json_print(
                {
                    "project": project.name,
                    "project_root": str(project.root),
                    "current_stream": {"stream_id": stream.id, "stream_label": stream.label},
                    "active_checkpoint": dict(checkpoint) if checkpoint else None,
                    "peer_streams": [item for item in streams if item.get("stream_id") != stream.id],
                    "peer_conflicts": peer_conflict_warnings(db, project, stream.id, checkpoint=checkpoint),
                    "recent_events": [dict(row) for row in events],
                }
            )
        finally:
            db.close()
        return 0

    if args.command == "compact":
        db = connect(codex_home)
        project = project_from_cli(getattr(args, "cwd", None))
        config = load_runtime_config(codex_home)
        try:
            stream = stream_from_cli_args(args, project, config, db)
            if args.compact_command == "status":
                result = compact_status(db, project, stream)
            else:
                result = compact_audit(db, project, stream, limit=args.limit)
            result["compact_context_smoke_passed"] = bool(config.get("compact_context_smoke_passed"))
            result["compact_resume_injection"] = bool(config.get("compact_resume_injection"))
            result["capture_only"] = not (result["compact_context_smoke_passed"] and result["compact_resume_injection"])
            json_print(result)
        finally:
            db.close()
        return 0

    if args.command == "quarantine":
        db = connect(codex_home)
        project = project_from_cli(getattr(args, "cwd", None)) if getattr(args, "cwd", None) else None
        try:
            if args.quarantine_command == "list":
                json_print(
                    {
                        "project": project.name if project else None,
                        "project_root": str(project.root) if project else None,
                        "rows": list_quarantine(db, project, limit=args.limit),
                    }
                )
                return 0
            if args.quarantine_command == "claim":
                changed = set_quarantine(db, args.table, args.row_id, reason=None, foreign_project_hint=None)
                json_print({"claimed": changed, "table": args.table, "row_id": args.row_id})
                return 0
            if args.quarantine_command == "release":
                changed = set_quarantine(
                    db,
                    args.table,
                    args.row_id,
                    reason=args.reason,
                    foreign_project_hint=args.foreign_project_hint,
                )
                json_print({"released": changed, "table": args.table, "row_id": args.row_id, "reason": args.reason})
                return 0
        finally:
            db.close()

    if args.command == "memory-candidates":
        db = connect(codex_home)
        project = project_from_cli(args.cwd) if args.cwd else None
        try:
            json_print(
                {
                    "project": project.name if project else None,
                    "project_root": str(project.root) if project else None,
                    "memory_candidates": list_memory_candidates(
                        db,
                        project,
                        include_quarantined=args.include_quarantined,
                        limit=args.limit,
                    ),
                }
            )
        finally:
            db.close()
        return 0

    if args.command == "migrate":
        if args.migrate_command == "codex-context":
            inspection = inspect_codex_context(codex_home)
            hook_entries = installer.codex_context_hook_entries(codex_home)
            inspection["codex_context_hook_entries"] = hook_entries
            config_path = codex_home / "config.toml"
            inspection["mcp_config"] = {
                "path": str(config_path),
                "exists": config_path.exists(),
                "mentions_codex_context": "codex_context" in config_path.read_text(encoding="utf-8")
                if config_path.exists()
                else False,
            }
            if args.dry_run:
                inspection["dry_run"] = True
                inspection["writes"] = []
                json_print(inspection)
                return 0
            backup = backup_codex_context_state(codex_home)
            db = connect(codex_home)
            try:
                imported = import_codex_context(
                    codex_home,
                    sentinel_db=db,
                    redact=bool(load_runtime_config(codex_home).get("redact", True)),
                )
            finally:
                db.close()
            hooks = installer.replace_codex_context_hooks(codex_home)
            manifest = {
                "version": VERSION,
                "migration": "codex-context",
                "created_at": None,
                "backup": backup,
                "inspection": inspection,
                "import": imported,
                "hooks": hooks,
            }
            from .core import utc_now

            manifest["created_at"] = utc_now()
            rollback = write_migration_rollback(codex_home, manifest)
            manifest["rollback_manifest"] = str(rollback)
            json_print(manifest)
            return 0

    if args.command == "retention":
        config = load_runtime_config(codex_home)
        config["retention_days"] = max(0, int(args.days))
        write_runtime_config(config, codex_home)
        json_print({"retention_days": config["retention_days"]})
        return 0

    if args.command == "config":
        config = load_runtime_config(codex_home)
        if args.config_command == "show":
            json_print(config)
            return 0
        if args.config_command == "set":
            config[str(args.key)] = validate_config_update(
                str(args.key),
                parse_config_value(str(args.value)),
            )
            write_runtime_config(config, codex_home)
            json_print({"updated": args.key, "value": config[str(args.key)]})
            return 0

    if args.command == "backup":
        if args.backup_command == "list":
            json_print({"backups": installer.list_backups(codex_home)})
            return 0
        if args.backup_command == "restore":
            json_print(installer.restore_backup(codex_home, args.backup_id))
            return 0

    if args.command == "mcp":
        from .mcp_server import loop

        return loop(codex_home=codex_home)

    parser.print_help()
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"compaction-sentinel: {exc}", file=sys.stderr)
        raise SystemExit(1)
