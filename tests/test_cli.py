from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def run_cli(self, codex_home: Path, *args: str, cwd: Path) -> str:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "compaction_sentinel.cli",
                "--codex-home",
                str(codex_home),
                *args,
            ],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_stream_claim_label_alias_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            self.run_cli(
                home,
                "stream",
                "claim",
                "--cwd",
                str(project),
                "--stream",
                "phase-9b",
                "--label",
                "Phase 9B hearing perception",
                cwd=project,
            )
            self.run_cli(
                home,
                "checkpoint",
                "--cwd",
                str(project),
                "--stream",
                "phase-9b",
                "--objective",
                "CLI stream objective",
                cwd=project,
            )
            status = json.loads(
                self.run_cli(
                    home,
                    "stream",
                    "status",
                    "--cwd",
                    str(project),
                    "--stream",
                    "phase-9b",
                    cwd=project,
                )
            )
            self.assertEqual(status["current_stream"]["stream_id"], "phase-9b")
            self.assertEqual(status["current_stream"]["stream_label"], "Phase 9B hearing perception")
            self.assertIn("CLI stream objective", status["active_checkpoint"]["objective"])

    def test_migrate_codex_context_dry_run_apply_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir(parents=True)
            (project / ".git").mkdir()
            context_dir = home / "codex-context"
            context_dir.mkdir(parents=True)
            source_db = context_dir / "context.db"
            source = sqlite3.connect(source_db)
            source.executescript(
                """
                CREATE TABLE records (
                  id INTEGER PRIMARY KEY,
                  project_path TEXT,
                  kind TEXT,
                  title TEXT,
                  content TEXT,
                  source_path TEXT,
                  tags TEXT,
                  importance TEXT,
                  content_hash TEXT,
                  updated_at TEXT
                );
                CREATE TABLE notes (
                  id INTEGER PRIMARY KEY,
                  project_path TEXT,
                  content TEXT,
                  surface_condition TEXT,
                  status TEXT,
                  created_at TEXT,
                  updated_at TEXT
                );
                CREATE TABLE checkpoints (
                  id INTEGER PRIMARY KEY,
                  project_path TEXT,
                  session_id TEXT,
                  cwd TEXT,
                  transcript_path TEXT,
                  event_name TEXT,
                  summary TEXT,
                  prompt_excerpt TEXT,
                  response_excerpt TEXT,
                  payload_hash TEXT,
                  created_at TEXT
                );
                """
            )
            source.execute(
                "INSERT INTO records (project_path, kind, title, content, tags, importance, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(project), "historian", "Historian", "Durable imported memory", "codex", "5", "record-hash"),
            )
            source.execute(
                "INSERT INTO notes (project_path, content, surface_condition, status) VALUES (?, ?, ?, ?)",
                (str(project), "Imported note", "when resuming", "open"),
            )
            source.execute(
                """
                INSERT INTO checkpoints
                  (project_path, session_id, cwd, transcript_path, event_name, summary, prompt_excerpt, response_excerpt, payload_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(project),
                    "s1",
                    str(project),
                    str(project / "transcript.jsonl"),
                    "UserPromptSubmit",
                    "Current step was imported",
                    "set goal: Finish imported migration",
                    "Imported evidence",
                    "checkpoint-hash",
                ),
            )
            source.commit()
            source.close()
            home.mkdir(exist_ok=True)
            (home / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": f"/usr/bin/python3 {context_dir}/codex_context.py hook UserPromptSubmit",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            (home / "config.toml").write_text(
                "[mcp_servers.codex_context]\ncommand = \"python3\"\n",
                encoding="utf-8",
            )

            dry = json.loads(self.run_cli(home, "migrate", "codex-context", "--dry-run", cwd=project))
            self.assertTrue(dry["dry_run"])
            self.assertEqual(dry["counts"]["records"], 1)
            self.assertFalse((home / "compaction-sentinel" / "sentinel.sqlite").exists())
            self.assertFalse((home / "backups").exists())

            applied = json.loads(self.run_cli(home, "migrate", "codex-context", "--apply", cwd=project))
            self.assertTrue(Path(applied["rollback_manifest"]).exists())
            hooks_text = (home / "hooks.json").read_text(encoding="utf-8")
            self.assertIn("compaction-sentinel", hooks_text)
            self.assertNotIn("codex_context.py", hooks_text)
            db = sqlite3.connect(home / "compaction-sentinel" / "sentinel.sqlite")
            try:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 500)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1)
            finally:
                db.close()

            self.run_cli(home, "migrate", "codex-context", "--apply", cwd=project)
            db = sqlite3.connect(home / "compaction-sentinel" / "sentinel.sqlite")
            try:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT status FROM checkpoints").fetchone()[0], "active")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
