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
    get_state,
    handle_hook,
    looks_complete,
    project_from_payload,
    project_state_prefix,
    redact_text,
    save_checkpoint,
    scrub_project,
)


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
            save_checkpoint(db, payload_project, objective="Finish verification", next_action="Run first check")
            db.close()
            first = handle_hook(
                "Stop",
                {"cwd": str(project), "session_id": "s1", "turn_id": "t1", "last_assistant_message": "Still working."},
                codex_home=home,
            )
            db = connect(home)
            save_checkpoint(db, payload_project, objective="Finish verification", next_action="Run second check")
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
            save_checkpoint(db, payload_project, objective="Finish verification", next_action="Run check")
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
            save_checkpoint(db, payload_project, objective="Finish verification", next_action="Run check")
            db.close()
            out = handle_hook(
                "Stop",
                {"cwd": str(project), "session_id": "s1", "turn_id": "t1", "last_assistant_message": "Still working."},
                codex_home=home,
            )
            self.assertEqual(out.get("decision"), "block")

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
            save_checkpoint(db, project, objective="Finish verification", next_action="Run check")
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
            remaining = db.execute("SELECT COUNT(*) AS count FROM state").fetchone()
            db.close()
            self.assertGreater(counts["state"], 0)
            self.assertEqual(int(remaining["count"]), 0)


if __name__ == "__main__":
    unittest.main()
