from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from compaction_sentinel.core import (
    active_checkpoint,
    build_resume_packet,
    connect,
    handle_hook,
    loop_warnings,
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


if __name__ == "__main__":
    unittest.main()
