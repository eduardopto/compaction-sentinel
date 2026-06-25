from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from compaction_sentinel.core import (
    active_checkpoint,
    build_resume_packet,
    connect,
    current_compaction_epoch,
    event_category,
    export_project,
    get_state,
    handle_hook,
    hash_text,
    infer_objective,
    is_failure_summary,
    looks_complete,
    maintenance_last_retention_key,
    project_from_payload,
    project_prune_count_key,
    project_state_prefix,
    list_streams,
    list_quarantine,
    peer_conflict_warnings,
    quarantine_count,
    redact_text,
    record_event,
    save_checkpoint,
    append_checkpoint_field,
    scrub_project,
    set_quarantine,
    stream_from_payload,
)


def session_stream(session_id: str) -> str:
    return "session:" + hash_text(session_id)[:24]


class CoreTests(unittest.TestCase):
    def test_prompt_hook_infers_goal_and_returns_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            payload = {
                "cwd": str(project),
                "session_id": "s1",
                "turn_id": "t1",
                "prompt": "set goal: build a flawless continuity package for Codex Desktop",
            }
            out = handle_hook("UserPromptSubmit", payload, codex_home=home)
            packet = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("build a flawless continuity package", packet)
            db = connect(home)
            checkpoint = active_checkpoint(db, project_from_payload(payload))
            self.assertIsNotNone(checkpoint)
            self.assertIn("continuity package", checkpoint["objective"])
            db.close()

    def test_migrates_existing_v02_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            db_file = home / "compaction-sentinel" / "sentinel.sqlite"
            db_file.parent.mkdir(parents=True)
            legacy = sqlite3.connect(db_file)
            legacy.executescript(
                """
                CREATE TABLE events (
                  id INTEGER PRIMARY KEY,
                  project_root TEXT NOT NULL,
                  project_name TEXT NOT NULL,
                  session_id TEXT,
                  turn_id TEXT,
                  event_name TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  details_json TEXT NOT NULL,
                  fingerprint TEXT,
                  outcome TEXT,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE checkpoints (
                  id INTEGER PRIMARY KEY,
                  project_root TEXT NOT NULL,
                  project_name TEXT NOT NULL,
                  session_id TEXT,
                  turn_id TEXT,
                  status TEXT NOT NULL DEFAULT 'active',
                  objective TEXT NOT NULL,
                  current_step TEXT,
                  next_action TEXT,
                  blockers TEXT,
                  evidence TEXT,
                  source TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT
                );
                CREATE TABLE notes (
                  id INTEGER PRIMARY KEY,
                  project_root TEXT NOT NULL,
                  project_name TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'open',
                  content TEXT NOT NULL,
                  surface_condition TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
                """
            )
            legacy.close()
            db = connect(home)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(events)").fetchall()}
            self.assertIn("event_key", columns)
            self.assertIn("stream_id", columns)
            note_columns = {row["name"] for row in db.execute("PRAGMA table_info(notes)").fetchall()}
            self.assertIn("stream_id", note_columns)
            indexes = {row["name"] for row in db.execute("PRAGMA index_list(events)").fetchall()}
            self.assertIn("idx_events_created_at", indexes)
            self.assertIn("idx_events_project_stream_id", indexes)
            db.close()

    def test_v05_schema_user_version_and_metadata_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            db = connect(home)
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, 500)
                for table in ("events", "checkpoints", "notes"):
                    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
                    self.assertIn("compaction_epoch", columns)
                    self.assertIn("source_system", columns)
                    self.assertIn("source_ref", columns)
                    self.assertIn("foreign_project_hint", columns)
                    self.assertIn("quarantine_reason", columns)
                    self.assertIn("thread_id", columns)
                memory_columns = {row["name"] for row in db.execute("PRAGMA table_info(memory_candidates)").fetchall()}
                self.assertIn("source_ref", memory_columns)
                self.assertIn("quarantine_reason", memory_columns)
            finally:
                db.close()

    def test_stream_fallback_order_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                explicit = stream_from_payload(
                    {"cwd": str(project_path), "stream_id": "Explicit Lane", "thread_id": "thread-a"},
                    project,
                    db=db,
                )
                self.assertEqual(explicit.id, "explicit-lane")
                thread = stream_from_payload({"cwd": str(project_path), "thread_id": "thread-a"}, project, db=db)
                self.assertEqual(thread.id, "thread-a")
                mapped = stream_from_payload(
                    {"cwd": str(project_path), "stream_id": "claimed", "session_id": "s2"},
                    project,
                    db=db,
                )
                self.assertEqual(mapped.id, "claimed")
                self.assertEqual(stream_from_payload({"cwd": str(project_path), "session_id": "s2"}, project, db=db).id, "claimed")
                transcript = stream_from_payload(
                    {"cwd": str(project_path), "stream_id": "transcript-lane", "transcript_path": "/tmp/transcript.jsonl"},
                    project,
                    db=db,
                )
                self.assertEqual(transcript.id, "transcript-lane")
                self.assertEqual(
                    stream_from_payload({"cwd": str(project_path), "transcript_path": "/tmp/transcript.jsonl"}, project, db=db).id,
                    "transcript-lane",
                )
                self.assertEqual(stream_from_payload({"cwd": str(project_path), "session_id": "s3"}, project, db=db).id, session_stream("s3"))
                self.assertEqual(stream_from_payload({"cwd": str(project_path)}, project, db=db).id, "default")
            finally:
                db.close()

    def test_stream_checkpoints_do_not_supersede_peer_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                save_checkpoint(
                    db,
                    project,
                    objective="Agent A objective",
                    next_action="Agent A next action",
                    stream_id="session-a",
                    stream_label="Phase 9A",
                )
                save_checkpoint(
                    db,
                    project,
                    objective="Agent B objective",
                    next_action="Agent B next action",
                    stream_id="session-b",
                    stream_label="Phase 9B",
                )
                save_checkpoint(
                    db,
                    project,
                    objective="Agent A refreshed objective",
                    next_action="Agent A refreshed next action",
                    stream_id="session-a",
                )
                a = active_checkpoint(db, project, stream_id="session-a")
                b = active_checkpoint(db, project, stream_id="session-b")
                self.assertIsNotNone(a)
                self.assertIsNotNone(b)
                self.assertIn("Agent A refreshed", a["objective"])
                self.assertIn("Agent B objective", b["objective"])
                self.assertIn("Agent B next action", b["next_action"])
            finally:
                db.close()

    def test_stream_packet_uses_current_stream_and_peers_are_awareness_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                save_checkpoint(
                    db,
                    project,
                    objective="Agent A hearing perception",
                    next_action="Implement hearing debug overlay",
                    files_touched="src/audio/hearing.ts",
                    stream_id="session-a",
                    stream_label="Phase 9B hearing",
                )
                save_checkpoint(
                    db,
                    project,
                    objective="Agent B combat perception",
                    next_action="Rewrite combat controller",
                    files_touched="src/combat/controller.ts",
                    stream_id="session-b",
                    stream_label="Phase 9C combat",
                )
                packet = build_resume_packet(db, project, stream_id="session-a")
            finally:
                db.close()
            self.assertIn("<stream>", packet)
            self.assertIn("session-a", packet)
            self.assertIn("Agent A hearing perception", packet)
            self.assertIn("<peer_workstreams awareness=\"only\">", packet)
            self.assertIn("Agent B combat perception", packet)
            self.assertIn("Peer workstreams are awareness only", packet)
            self.assertIn("Implement hearing debug overlay", packet)
            self.assertNotIn("<next_action>\nRewrite combat controller", packet)

    def test_new_hook_session_does_not_adopt_existing_non_default_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                save_checkpoint(
                    db,
                    project,
                    objective="Existing Agent A objective",
                    next_action="Agent A only",
                    stream_id="agent-a-session",
                )
            finally:
                db.close()
            out = handle_hook(
                "UserPromptSubmit",
                {
                    "cwd": str(project_path),
                    "session_id": "agent-b-session",
                    "turn_id": "t1",
                    "prompt": "set goal: Existing Agent B objective",
                },
                codex_home=home,
            )
            packet = out["hookSpecificOutput"]["additionalContext"]
            db = connect(home)
            try:
                a = active_checkpoint(db, project, stream_id="agent-a-session")
                b = active_checkpoint(db, project, stream_id=session_stream("agent-b-session"))
            finally:
                db.close()
            self.assertIn(session_stream("agent-b-session"), packet)
            self.assertIn("Existing Agent B objective", packet)
            self.assertIsNotNone(a)
            self.assertIsNotNone(b)
            self.assertIn("Existing Agent A objective", a["objective"])
            self.assertIn("Existing Agent B objective", b["objective"])

    def test_legacy_default_stream_is_owned_by_first_session_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                save_checkpoint(
                    db,
                    project,
                    objective="Legacy default objective",
                    next_action="Legacy next action",
                )
            finally:
                db.close()
            first = handle_hook(
                "SessionStart",
                {"cwd": str(project_path), "session_id": "agent-a-session"},
                codex_home=home,
            )
            self.assertIn("Legacy default objective", first["hookSpecificOutput"]["additionalContext"])
            second = handle_hook(
                "UserPromptSubmit",
                {
                    "cwd": str(project_path),
                    "session_id": "agent-b-session",
                    "turn_id": "t1",
                    "prompt": "set goal: Fresh Agent B work",
                },
                codex_home=home,
            )
            db = connect(home)
            try:
                default = active_checkpoint(db, project, stream_id="default")
                b = active_checkpoint(db, project, stream_id=session_stream("agent-b-session"))
            finally:
                db.close()
            self.assertIn(session_stream("agent-b-session"), second["hookSpecificOutput"]["additionalContext"])
            self.assertIsNotNone(default)
            self.assertIsNotNone(b)
            self.assertIn("Legacy default objective", default["objective"])
            self.assertIn("Fresh Agent B work", b["objective"])

    def test_stream_evidence_scrub_and_export_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                save_checkpoint(db, project, objective="Agent A objective", stream_id="session-a")
                save_checkpoint(db, project, objective="Agent B objective", stream_id="session-b")
                append_checkpoint_field(
                    db,
                    project,
                    field="evidence",
                    value="Agent A proof",
                    stream_id="session-a",
                )
                a = active_checkpoint(db, project, stream_id="session-a")
                b = active_checkpoint(db, project, stream_id="session-b")
                self.assertIn("Agent A proof", a["evidence"])
                self.assertNotIn("Agent A proof", b["evidence"] or "")
                export_a = export_project(db, project, stream_id="session-a")
                export_all = export_project(db, project)
                self.assertEqual({row["stream_id"] for row in export_a["checkpoints"]}, {"session-a"})
                self.assertEqual({row["stream_id"] for row in export_all["checkpoints"]}, {"session-a", "session-b"})
                scrubbed = scrub_project(db, project, stream_id="session-a")
                self.assertGreater(scrubbed["checkpoints"], 0)
                self.assertIsNone(active_checkpoint(db, project, stream_id="session-a"))
                self.assertIsNotNone(active_checkpoint(db, project, stream_id="session-b"))
                scrub_project(db, project)
                self.assertIsNone(active_checkpoint(db, project, stream_id="session-b"))
            finally:
                db.close()

    def test_peer_conflict_warning_is_awareness_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                save_checkpoint(
                    db,
                    project,
                    objective="Agent A path",
                    next_action="Edit current stream",
                    files_touched="src/shared/router.ts",
                    stream_id="session-a",
                    stream_label="A",
                )
                save_checkpoint(
                    db,
                    project,
                    objective="Agent B path",
                    next_action="Peer next action must not become active",
                    files_touched="src/shared/router.ts",
                    stream_id="session-b",
                    stream_label="B",
                )
                warnings = peer_conflict_warnings(db, project, "session-a")
                streams = list_streams(db, project)
                packet = build_resume_packet(db, project, stream_id="session-a")
            finally:
                db.close()
            self.assertTrue(any("also touches" in warning for warning in warnings))
            self.assertEqual({item["stream_id"] for item in streams}, {"session-a", "session-b"})
            self.assertIn("<peer_conflicts awareness=\"only\">", packet)
            self.assertIn("Agent B path", packet)
            self.assertNotIn("<next_action>\nPeer next action must not become active", packet)

    def test_compact_hooks_capture_only_and_compact_session_start_is_non_intrusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            payload = {"cwd": str(project_path), "session_id": "s1", "source": "auto"}
            pre = handle_hook("PreCompact", payload, codex_home=home)
            post = handle_hook("PostCompact", payload, codex_home=home)
            compact_start = handle_hook(
                "SessionStart",
                {"cwd": str(project_path), "session_id": "s1", "source": "compact"},
                codex_home=home,
            )
            self.assertEqual(pre, {})
            self.assertEqual(post, {})
            self.assertEqual(compact_start, {})
            db = connect(home)
            try:
                project = project_from_payload(payload)
                self.assertEqual(current_compaction_epoch(db, project, session_stream("s1")), 1)
                rows = db.execute("SELECT event_name, compaction_epoch FROM events ORDER BY id").fetchall()
            finally:
                db.close()
            self.assertEqual([row["event_name"] for row in rows], ["PreCompact", "PostCompact", "SessionStart"])
            self.assertEqual([row["compaction_epoch"] for row in rows[:2]], [1, 1])

    def test_quarantined_foreign_checkpoint_is_excluded_until_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            foreign_path = Path(tmp) / "foreign"
            project_path.mkdir()
            foreign_path.mkdir()
            (project_path / ".git").mkdir()
            (foreign_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                save_checkpoint(db, project, objective="Local objective", next_action="Stay local")
                foreign_id = save_checkpoint(
                    db,
                    project,
                    objective=f"Implement active work in {foreign_path}/app.py",
                    next_action="Do foreign work",
                )
                active = active_checkpoint(db, project)
                packet = build_resume_packet(db, project)
                quarantined = list_quarantine(db, project)
                self.assertEqual(active["objective"], "Local objective")
                self.assertNotIn("Do foreign work", packet)
                self.assertEqual(quarantine_count(db, project), 1)
                self.assertEqual(quarantined[0]["table"], "checkpoints")
                self.assertTrue(set_quarantine(db, "checkpoints", foreign_id, reason=None))
                self.assertEqual(active_checkpoint(db, project)["objective"], f"Implement active work in {foreign_path}/app.py")
                self.assertTrue(set_quarantine(db, "checkpoints", foreign_id, reason="manual_quarantine"))
                self.assertEqual(active_checkpoint(db, project)["objective"], "Local objective")
            finally:
                db.close()

    def test_benign_foreign_reference_read_is_not_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            foreign_path = Path(tmp) / "foreign"
            project_path.mkdir()
            foreign_path.mkdir()
            (project_path / ".git").mkdir()
            (foreign_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                row = record_event(
                    db,
                    project,
                    {
                        "cwd": str(project_path),
                        "tool_name": "Bash",
                        "tool_input": {"command": f"sed -n '1,20p' {foreign_path}/README.md"},
                    },
                    event_name="PreToolUse",
                    kind="tool:Bash",
                    summary=f"sed -n '1,20p' {foreign_path}/README.md",
                )
                self.assertIsNone(row["quarantine_reason"])
                self.assertEqual(quarantine_count(db, project), 0)
            finally:
                db.close()

    def test_retention_is_throttled_but_still_runs_when_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                first = record_event(
                    db,
                    project,
                    {"cwd": str(project_path), "tool_use_id": "old"},
                    event_name="PreToolUse",
                    kind="tool:Bash",
                    summary="pytest old",
                    retention_days=1,
                    retention_check_interval_seconds=3600,
                )
                db.execute("UPDATE events SET created_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", first["id"]))
                db.commit()
                record_event(
                    db,
                    project,
                    {"cwd": str(project_path), "tool_use_id": "skip"},
                    event_name="PreToolUse",
                    kind="tool:Bash",
                    summary="pytest skip retention",
                    retention_days=1,
                    retention_check_interval_seconds=3600,
                )
                old_count = db.execute("SELECT COUNT(*) AS count FROM events WHERE id = ?", (first["id"],)).fetchone()
                self.assertEqual(int(old_count["count"]), 1)
                self.assertIsNotNone(get_state(db, maintenance_last_retention_key()))
                record_event(
                    db,
                    project,
                    {"cwd": str(project_path), "tool_use_id": "due"},
                    event_name="PreToolUse",
                    kind="tool:Bash",
                    summary="pytest force retention",
                    retention_days=1,
                    retention_check_interval_seconds=0,
                )
                old_count = db.execute("SELECT COUNT(*) AS count FROM events WHERE id = ?", (first["id"],)).fetchone()
                self.assertEqual(int(old_count["count"]), 0)
            finally:
                db.close()

    def test_project_prune_is_throttled_by_event_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            try:
                save_checkpoint(db, project, objective="Keep checkpoint", next_action="Keep working")
                for index in range(20):
                    record_event(
                        db,
                        project,
                        {"cwd": str(project_path), "tool_use_id": f"tool-{index}"},
                        event_name="PreToolUse",
                        kind="tool:Bash",
                        summary=f"pytest test_{index}",
                        max_events=3,
                        prune_check_interval_events=5,
                    )
                event_count = db.execute("SELECT COUNT(*) AS count FROM events WHERE project_root = ?", (str(project.root),)).fetchone()
                checkpoint_count = db.execute("SELECT COUNT(*) AS count FROM checkpoints WHERE project_root = ?", (str(project.root),)).fetchone()
                self.assertLessEqual(int(event_count["count"]), 3)
                self.assertEqual(int(checkpoint_count["count"]), 1)
                self.assertEqual(get_state(db, project_prune_count_key(project)), "0")
            finally:
                db.close()

    def test_loop_warning_after_repeated_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            payload = {
                "cwd": str(project),
                "session_id": "s1",
                "turn_id": "t1",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest tests/test_same.py"},
            }
            handle_hook("PreToolUse", payload, codex_home=home)
            handle_hook("PreToolUse", payload, codex_home=home)
            out = handle_hook("PreToolUse", payload, codex_home=home)
            self.assertIn("Compaction Sentinel loop warning", out["hookSpecificOutput"]["additionalContext"])

    def test_redaction(self) -> None:
        text = redact_text("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", text)
        self.assertIn("[redacted]", text)
        text = redact_text("github_pat_abcdefghijklmnopqrstuvwxyz1234567890")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", text)
        self.assertIn("[redacted]", text)

    def test_resume_packet_includes_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            payload_project = project_from_payload({"cwd": str(project)})
            db = connect(home)
            save_checkpoint(
                db,
                payload_project,
                objective="Finish device install verification",
                current_step="Build passed",
                next_action="Install on phone",
                evidence="xcodebuild succeeded",
            )
            packet = build_resume_packet(db, payload_project)
            self.assertIn("Finish device install verification", packet)
            self.assertIn("Install on phone", packet)
            self.assertIn("schema=\"packet-v2\"", packet)
            self.assertIn("<next_action>", packet)
            db.close()

    def test_complete_checkpoint_closes_active_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            payload_project = project_from_payload({"cwd": str(project)})
            db = connect(home)
            save_checkpoint(db, payload_project, objective="Ship the thing")
            self.assertIsNotNone(active_checkpoint(db, payload_project))
            save_checkpoint(db, payload_project, objective="Ship the thing", status="complete")
            self.assertIsNone(active_checkpoint(db, payload_project))
            db.close()

    def test_completed_checkpoint_is_historical_not_active_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config_path = home / "compaction-sentinel" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"auto_continue": "gentle", "stop_continue_max_per_turn": 1}),
                encoding="utf-8",
            )
            payload_project = project_from_payload({"cwd": str(project)})
            db = connect(home)
            try:
                save_checkpoint(
                    db,
                    payload_project,
                    objective="Ship completed work",
                    current_step="Implemented the completed work.",
                    next_action="Run stale completed next action",
                    evidence="Completed proof passed.",
                )
                save_checkpoint(
                    db,
                    payload_project,
                    objective="Ship completed work",
                    status="complete",
                    current_step="Completed work was verified.",
                    next_action="Run stale completed next action",
                    evidence="Final verification passed.",
                )
                packet = build_resume_packet(db, payload_project)
                tiny_packet = build_resume_packet(db, payload_project, max_chars=1000)
            finally:
                db.close()
            self.assertIn("<active_objective>\nstatus: none", packet)
            self.assertIn("<last_checkpoint>", packet)
            self.assertIn("historical context only", packet)
            self.assertIn("Ship completed work", packet)
            self.assertNotIn("<next_action>\nRun stale completed next action", packet)
            self.assertNotIn("Run stale completed next action", packet)
            self.assertIn("status: none", tiny_packet)
            self.assertIn("<last_checkpoint>", tiny_packet)
            out = handle_hook(
                "Stop",
                {
                    "cwd": str(project),
                    "session_id": "s1",
                    "turn_id": "t1",
                    "last_assistant_message": "Still working.",
                },
                codex_home=home,
            )
            self.assertEqual(out, {})

    def test_stop_hook_noop_returns_empty_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            out = handle_hook("Stop", {"cwd": str(project), "last_assistant_message": "Done."}, codex_home=home)
            self.assertEqual(out, {})

    def test_permission_request_records_without_auto_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            payload = {
                "cwd": str(project),
                "session_id": "s1",
                "turn_id": "t1",
                "tool_use_id": "approval-1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf build", "description": "cleanup generated output"},
            }
            out = handle_hook("PermissionRequest", payload, codex_home=home)
            self.assertNotIn("decision", out)
            db = connect(home)
            events = [dict(row) for row in db.execute("SELECT * FROM events").fetchall()]
            db.close()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_name"], "PermissionRequest")
            self.assertIn("rm -rf build", events[0]["summary"])

    def test_repeated_permission_request_warns_without_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            for index in range(3):
                payload = {
                    "cwd": str(project),
                    "session_id": "s1",
                    "turn_id": "t1",
                    "tool_use_id": f"approval-{index}",
                    "tool_name": "Bash",
                    "tool_input": {"command": "sudo rm -rf build", "description": "cleanup generated output"},
                }
                out = handle_hook("PermissionRequest", payload, codex_home=home)
            self.assertNotIn("decision", out)
            self.assertIn("Repeated permission request detected", out.get("systemMessage", ""))

    def test_stop_continue_max_per_turn_and_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config_path = home / "compaction-sentinel" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"auto_continue": "gentle", "stop_continue_max_per_turn": 1}),
                encoding="utf-8",
            )
            payload_project = project_from_payload({"cwd": str(project)})
            db = connect(home)
            save_checkpoint(
                db,
                payload_project,
                objective="Finish verification",
                current_step="Smoke passed",
                next_action="Run one more check",
                evidence="smoke passed",
                stream_id=session_stream("s1"),
            )
            db.close()
            payload = {"cwd": str(project), "session_id": "s1", "turn_id": "t1", "last_assistant_message": "Still working."}
            first = handle_hook("Stop", payload, codex_home=home)
            second = handle_hook("Stop", payload, codex_home=home)
            self.assertEqual(first.get("decision"), "block")
            self.assertEqual(second, {})

    def test_stop_continue_session_turn_cap_survives_checkpoint_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config_path = home / "compaction-sentinel" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "auto_continue": "gentle",
                        "stop_continue_max_per_turn": 1,
                        "stop_continue_max_per_checkpoint_per_turn": 5,
                    }
                ),
                encoding="utf-8",
            )
            payload_project = project_from_payload({"cwd": str(project)})
            db = connect(home)
            save_checkpoint(
                db,
                payload_project,
                objective="Finish verification",
                current_step="Step one",
                next_action="Run first check",
                stream_id=session_stream("s1"),
            )
            db.close()
            payload = {"cwd": str(project), "session_id": "s1", "turn_id": "t1", "last_assistant_message": "Still working."}
            first = handle_hook("Stop", payload, codex_home=home)
            db = connect(home)
            save_checkpoint(
                db,
                payload_project,
                objective="Finish verification",
                current_step="Step two",
                next_action="Run second check",
                stream_id=session_stream("s1"),
            )
            db.close()
            second = handle_hook("Stop", payload, codex_home=home)
            self.assertEqual(first.get("decision"), "block")
            self.assertEqual(second, {})

    def test_stop_continue_cooldown_blocks_later_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config_path = home / "compaction-sentinel" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "auto_continue": "gentle",
                        "stop_continue_max_per_turn": 5,
                        "stop_continue_cooldown_seconds": 300,
                    }
                ),
                encoding="utf-8",
            )
            payload_project = project_from_payload({"cwd": str(project)})
            db = connect(home)
            save_checkpoint(db, payload_project, objective="Finish verification", next_action="Run first check", stream_id=session_stream("s1"))
            db.close()
            first = handle_hook(
                "Stop",
                {"cwd": str(project), "session_id": "s1", "turn_id": "t1", "last_assistant_message": "Still working."},
                codex_home=home,
            )
            db = connect(home)
            save_checkpoint(db, payload_project, objective="Finish verification", next_action="Run second check", stream_id=session_stream("s1"))
            db.close()
            second = handle_hook(
                "Stop",
                {"cwd": str(project), "session_id": "s1", "turn_id": "t2", "last_assistant_message": "Still working."},
                codex_home=home,
            )
            self.assertEqual(first.get("decision"), "block")
            self.assertEqual(second, {})

    def test_stop_continue_zero_turn_cap_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config_path = home / "compaction-sentinel" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"auto_continue": "gentle", "stop_continue_max_per_turn": 0}),
                encoding="utf-8",
            )
            payload_project = project_from_payload({"cwd": str(project)})
            db = connect(home)
            save_checkpoint(db, payload_project, objective="Finish verification", next_action="Run check", stream_id=session_stream("s1"))
            db.close()
            out = handle_hook(
                "Stop",
                {"cwd": str(project), "session_id": "s1", "turn_id": "t1", "last_assistant_message": "Still working."},
                codex_home=home,
            )
            self.assertEqual(out, {})

    def test_invalid_numeric_runtime_config_does_not_crash_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config_path = home / "compaction-sentinel" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "auto_continue": "gentle",
                        "loop_threshold": "bad",
                        "max_packet_chars": "bad",
                        "max_events_per_project": "bad",
                        "retention_days": "bad",
                        "stop_continue_max_per_turn": "bad",
                        "stop_continue_cooldown_seconds": "bad",
                    }
                ),
                encoding="utf-8",
            )
            payload_project = project_from_payload({"cwd": str(project)})
            db = connect(home)
            save_checkpoint(db, payload_project, objective="Finish verification", next_action="Run check", stream_id=session_stream("s1"))
            db.close()
            out = handle_hook(
                "Stop",
                {"cwd": str(project), "session_id": "s1", "turn_id": "t1", "last_assistant_message": "Still working."},
                codex_home=home,
            )
            self.assertEqual(out.get("decision"), "block")

    def test_goal_inference_ignores_quoted_examples(self) -> None:
        prompt = (
            "Please audit this project.\n\n"
            "Add a replay fixture or unit test that simulates a normal long task without \"set goal:\" "
            "and verifies an explicit MCP checkpoint."
        )
        self.assertIsNone(infer_objective(prompt))
        self.assertEqual(
            infer_objective("Goal:\nHarden the release without rewriting architecture."),
            "Harden the release without rewriting architecture.",
        )

    def test_reading_docs_with_failure_words_is_not_tool_failure(self) -> None:
        summary = (
            "Bash: sed -n '1,120p' docs/guard.md -> "
            "This guard prevents failed loops, exceptions, traceback text, and false failure claims."
        )
        self.assertFalse(is_failure_summary(summary))
        self.assertEqual(event_category("PostToolUse", "tool-result:Bash", summary), "tool_result")

    def test_read_only_command_real_file_error_is_failure(self) -> None:
        summary = "Bash: sed -n '1,120p' missing.md -> sed: missing.md: No such file or directory"
        self.assertTrue(is_failure_summary(summary))
        self.assertEqual(event_category("PostToolUse", "tool-result:Bash", summary), "tool_failure")
        self.assertTrue(is_failure_summary("Bash: git show missing-ref -> fatal: ambiguous argument 'missing-ref'"))

    def test_test_command_failure_still_counts_as_failure(self) -> None:
        summary = "Bash: pytest tests/test_app.py -> FAILED tests/test_app.py::test_save - AssertionError"
        self.assertTrue(is_failure_summary(summary))
        self.assertEqual(event_category("PostToolUse", "tool-result:Bash", summary), "tool_failure")

    def test_test_command_zero_failed_is_success_not_failure(self) -> None:
        summary = "Bash: pytest tests/test_app.py -> 61 passed, 0 failed, 22 skipped"
        self.assertFalse(is_failure_summary(summary))
        self.assertEqual(event_category("PostToolUse", "tool-result:Bash", summary), "tool_success")
        self.assertFalse(is_failure_summary("Bash: pytest tests/test_app.py -> 0 failed"))
        self.assertFalse(is_failure_summary("Bash: pytest tests/test_app.py -> no tests failed"))

    def test_read_only_validation_errors_still_count_as_failures(self) -> None:
        cases = [
            "Bash: jq . bad.json -> parse error: Invalid numeric literal at line 1, column 7",
            "Bash: python -m json.tool bad.json -> Expecting value: line 1 column 1 (char 0)",
            "Bash: rg '[' src -> regex parse error: unclosed character class",
        ]
        for summary in cases:
            with self.subTest(summary=summary):
                self.assertTrue(is_failure_summary(summary))

    def test_reading_source_error_examples_is_not_failure(self) -> None:
        summary = (
            "Bash: sed -n '1,160p' docs/guard.md -> "
            "Example output: sed: missing.md: No such file or directory. "
            "Example output: parse error: Invalid numeric literal."
        )
        self.assertFalse(is_failure_summary(summary))

    def test_repeated_doc_reads_do_not_emit_failure_loop_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            response = (
                "This source file contains words like failed, exception, traceback, "
                "fatal, and error as examples, but the read command succeeded."
            )
            out: dict[str, object] = {}
            for index in range(3):
                out = handle_hook(
                    "PostToolUse",
                    {
                        "cwd": str(project),
                        "session_id": "s1",
                        "turn_id": "t1",
                        "tool_use_id": f"doc-{index}",
                        "tool_name": "Bash",
                        "tool_input": {"command": "sed -n '1,120p' docs/guard.md"},
                        "tool_response": response,
                    },
                    codex_home=home,
                )
            self.assertEqual(out, {})
            db = connect(home)
            categories = [
                row["category"]
                for row in db.execute("SELECT category FROM events ORDER BY id").fetchall()
            ]
            db.close()
            self.assertEqual(categories, ["tool_result", "tool_result", "tool_result"])

    def test_read_only_tool_response_is_compacted_by_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config_path = home / "compaction-sentinel" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"max_read_only_response_chars": 120, "max_tool_response_chars": 300}),
                encoding="utf-8",
            )
            huge = "\n".join(f"line {index} with ordinary documentation text" for index in range(80))
            handle_hook(
                "PostToolUse",
                {
                    "cwd": str(project),
                    "session_id": "s1",
                    "turn_id": "t1",
                    "tool_use_id": "read-big",
                    "tool_name": "Bash",
                    "tool_input": {"command": "sed -n '1,200p' docs/large.md"},
                    "tool_response": huge,
                },
                codex_home=home,
            )
            db = connect(home)
            row = db.execute("SELECT summary, details_json FROM events ORDER BY id DESC LIMIT 1").fetchone()
            db.close()
            self.assertLess(len(row["summary"]), 260)
            self.assertLess(len(row["details_json"]), 420)
            self.assertIn("middle truncated", row["summary"])

    def test_failure_tool_response_keeps_error_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config_path = home / "compaction-sentinel" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"max_tool_response_chars": 260}), encoding="utf-8")
            noisy_failure = "\n".join(
                ["setup noise"] * 30
                + ["FAILED tests/test_app.py::test_save", "AssertionError: expected durable checkpoint"]
                + ["tail noise"] * 30
            )
            handle_hook(
                "PostToolUse",
                {
                    "cwd": str(project),
                    "session_id": "s1",
                    "turn_id": "t1",
                    "tool_use_id": "test-fail-big",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pytest tests/test_app.py"},
                    "tool_response": noisy_failure,
                },
                codex_home=home,
            )
            db = connect(home)
            row = db.execute("SELECT summary, category FROM events ORDER BY id DESC LIMIT 1").fetchone()
            db.close()
            self.assertEqual(row["category"], "tool_failure")
            self.assertIn("AssertionError", row["summary"])
            self.assertLess(len(row["summary"]), 420)

    def test_light_performance_mode_records_only_hot_milestones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config_path = home / "compaction-sentinel" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"performance_mode": "light"}), encoding="utf-8")
            handle_hook(
                "PostToolUse",
                {
                    "cwd": str(project),
                    "session_id": "s1",
                    "turn_id": "t1",
                    "tool_use_id": "read-light",
                    "tool_name": "Bash",
                    "tool_input": {"command": "sed -n '1,20p' README.md"},
                    "tool_response": "README contents",
                },
                codex_home=home,
            )
            handle_hook(
                "PostToolUse",
                {
                    "cwd": str(project),
                    "session_id": "s1",
                    "turn_id": "t1",
                    "tool_use_id": "fail-light",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pytest tests/test_app.py"},
                    "tool_response": "FAILED tests/test_app.py::test_save - AssertionError",
                },
                codex_home=home,
            )
            db = connect(home)
            rows = db.execute("SELECT summary, category FROM events ORDER BY id").fetchall()
            db.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["category"], "tool_failure")
            self.assertIn("pytest", rows[0]["summary"])

    def test_repeated_test_failures_still_emit_failure_loop_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            out: dict[str, object] = {}
            for index in range(3):
                out = handle_hook(
                    "PostToolUse",
                    {
                        "cwd": str(project),
                        "session_id": "s1",
                        "turn_id": "t1",
                        "tool_use_id": f"test-{index}",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest tests/test_app.py"},
                        "tool_response": "FAILED tests/test_app.py::test_save - AssertionError",
                    },
                    codex_home=home,
                )
            text = json.dumps(out)
            self.assertIn("Same failure loop", text)

    def test_stop_catches_tool_output_blindness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            handle_hook(
                "PostToolUse",
                {
                    "cwd": str(project),
                    "session_id": "s1",
                    "turn_id": "t1",
                    "tool_use_id": "failed-test",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pytest tests/test_app.py"},
                    "tool_response": "FAILED tests/test_app.py::test_save - AssertionError",
                },
                codex_home=home,
            )
            out = handle_hook(
                "Stop",
                {
                    "cwd": str(project),
                    "session_id": "s1",
                    "turn_id": "t1",
                    "last_assistant_message": "Goal complete. Completed and verified.",
                },
                codex_home=home,
            )
            self.assertIn("Tool-output blindness risk", json.dumps(out))

    def test_shell_wrappers_preserve_failure_classification(self) -> None:
        doc_response = "This file mentions error: fatal, traceback, exception, and failed as documentation text."
        read_commands = [
            "bash -lc \"sed -n '1,120p' docs/guard.md\"",
            "zsh -lc \"sed -n '1,120p' docs/guard.md\"",
        ]
        for command in read_commands:
            with self.subTest(command=command):
                summary = f"Bash: {command} -> {doc_response}"
                self.assertFalse(is_failure_summary(summary))
                self.assertEqual(event_category("PostToolUse", "tool-result:Bash", summary), "tool_result")

        failing = [
            'bash -lc "pytest tests/test_app.py" -> FAILED tests/test_app.py::test_save - AssertionError',
            "uv run pytest tests/test_app.py -> FAILED tests/test_app.py::test_save - AssertionError",
            "python -m pytest tests/test_app.py -> Traceback (most recent call last): AssertionError",
        ]
        for tail in failing:
            with self.subTest(tail=tail):
                summary = f"Bash: {tail}"
                self.assertTrue(is_failure_summary(summary))
                self.assertEqual(event_category("PostToolUse", "tool-result:Bash", summary), "tool_failure")

        success = "Bash: poetry run pytest tests/test_app.py -> 61 passed, 0 failed"
        self.assertFalse(is_failure_summary(success))
        self.assertIn(
            event_category("PostToolUse", "tool-result:Bash", success),
            {"tool_success", "tool_result"},
        )

    def test_tiny_packet_budgets_preserve_priority_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            save_checkpoint(
                db,
                project,
                objective="Tiny objective",
                current_step="A lot of less important current-state detail should be dropped first.",
                next_action="Run final check",
                blockers="Device locked",
                tests_passed="Strong proof passed",
                do_not_repeat="",
                stream_id=session_stream("s1"),
            )
            db.close()
            for index in range(3):
                handle_hook(
                    "PreToolUse",
                    {
                        "cwd": str(project_path),
                        "session_id": "s1",
                        "turn_id": "t1",
                        "tool_use_id": f"tiny-{index}",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest tests/tiny_budget.py"},
                    },
                    codex_home=home,
                )
            db = connect(home)
            try:
                for budget in (500, 1000, 2000):
                    packet = build_resume_packet(db, project, max_chars=budget)
                    self.assertLessEqual(len(packet), budget)
                    self.assertIn("Tiny objective", packet)
                    self.assertIn("Run final check", packet)
                    self.assertIn("Device locked", packet)
                    self.assertIn("Strong proof passed", packet)
                    self.assertIn("Same command loop", packet)
                    self.assertIn("<resume_contract>", packet)
                    self.assertNotIn("<recent_event_trail>", packet)
            finally:
                db.close()

    def test_completion_detection_rejects_partial_completion(self) -> None:
        self.assertTrue(looks_complete("All tests passed and CI is green."))
        self.assertFalse(looks_complete("Done with the first pass, but tests are still failing."))
        self.assertFalse(looks_complete("Completed the edit, not verified yet."))
        self.assertFalse(looks_complete("Could not finish because one blocker is remaining."))

    def test_scrub_project_removes_project_state_without_raw_path_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project_path = Path(tmp) / "repo"
            project_path.mkdir()
            (project_path / ".git").mkdir()
            config_path = home / "compaction-sentinel" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"auto_continue": "gentle", "stop_continue_max_per_turn": 2}),
                encoding="utf-8",
            )
            project = project_from_payload({"cwd": str(project_path)})
            db = connect(home)
            save_checkpoint(db, project, objective="Finish verification", next_action="Run check", stream_id=session_stream("s1"))
            db.close()
            out = handle_hook(
                "Stop",
                {"cwd": str(project_path), "session_id": "s1", "turn_id": "t1", "last_assistant_message": "Still working."},
                codex_home=home,
            )
            self.assertEqual(out.get("decision"), "block")
            db = connect(home)
            state_keys = [row["key"] for row in db.execute("SELECT key FROM state").fetchall()]
            self.assertTrue(any(key.startswith(project_state_prefix(project)) for key in state_keys))
            self.assertFalse(any(str(project.root) in key for key in state_keys))
            counts = scrub_project(db, project)
            remaining_project = [
                row["key"]
                for row in db.execute("SELECT key FROM state ORDER BY key").fetchall()
                if str(row["key"]).startswith(project_state_prefix(project))
            ]
            db.close()
            self.assertGreater(counts["state"], 0)
            self.assertEqual(remaining_project, [])


if __name__ == "__main__":
    unittest.main()
