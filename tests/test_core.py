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
    handle_hook,
    project_from_payload,
    redact_text,
    save_checkpoint,
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


if __name__ == "__main__":
    unittest.main()
