from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class McpTests(unittest.TestCase):
    def run_mcp(self, codex_home: Path, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "compaction_sentinel.cli",
                "--codex-home",
                str(codex_home),
                "mcp",
            ],
            input="\n".join(json.dumps(message) for message in messages) + "\n",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return [json.loads(line) for line in process.stdout.splitlines() if line.strip()]

    def test_mcp_tools_require_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            responses = self.run_mcp(
                home,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "compaction_status", "arguments": {}},
                    }
                ],
            )
            self.assertIn("cwd is required", responses[0]["error"]["message"])

    def test_mcp_tool_schema_requires_cwd_and_call_succeeds_with_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            responses = self.run_mcp(
                home,
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_checkpoint",
                            "arguments": {"cwd": str(project), "objective": "MCP cwd contract"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "compaction_status", "arguments": {"cwd": str(project)}},
                    },
                ],
            )
            tools = responses[0]["result"]["tools"]
            for tool in tools:
                self.assertIn("cwd", tool["inputSchema"].get("required", []), tool["name"])
            self.assertIn("Saved Compaction Sentinel checkpoint", responses[1]["result"]["content"][0]["text"])
            self.assertIn("MCP cwd contract", responses[2]["result"]["content"][0]["text"])

    def test_mcp_checkpoint_honors_redaction_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config = home / "compaction-sentinel" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"redact": True}), encoding="utf-8")
            secret = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
            responses = self.run_mcp(
                home,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_checkpoint",
                            "arguments": {"cwd": str(project), "objective": secret},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "compaction_status", "arguments": {"cwd": str(project)}},
                    },
                ],
            )
            status_text = responses[1]["result"]["content"][0]["text"]
            self.assertIn("[redacted]", status_text)
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", status_text)

    def test_mcp_checkpoint_can_preserve_text_when_redaction_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config = home / "compaction-sentinel" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"redact": False}), encoding="utf-8")
            text = "Preserve non-secret acceptance detail for MCP redaction disabled mode"
            responses = self.run_mcp(
                home,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_checkpoint",
                            "arguments": {"cwd": str(project), "objective": text},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "compaction_status", "arguments": {"cwd": str(project)}},
                    },
                ],
            )
            self.assertIn(text, responses[1]["result"]["content"][0]["text"])

    def test_mcp_packet_uses_runtime_limits_and_safe_argument_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            config = home / "compaction-sentinel" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"max_packet_chars": 700, "loop_threshold": "bad"}),
                encoding="utf-8",
            )
            responses = self.run_mcp(
                home,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_checkpoint",
                            "arguments": {
                                "cwd": str(project),
                                "objective": "MCP packet budget objective " + ("details " * 80),
                                "next_action": "Keep this packet under the configured budget.",
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_packet",
                            "arguments": {"cwd": str(project), "max_chars": "not-an-int"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_search",
                            "arguments": {"cwd": str(project), "query": "missing", "limit": "bad"},
                        },
                    },
                ],
            )
            packet = responses[1]["result"]["content"][0]["text"]
            self.assertLessEqual(len(packet), 700)
            self.assertIn("MCP packet budget objective", packet)
            self.assertIn("No matching Compaction Sentinel events", responses[2]["result"]["content"][0]["text"])

    def test_explicit_mcp_checkpoint_for_normal_long_task_builds_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            responses = self.run_mcp(
                home,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_checkpoint",
                            "arguments": {
                                "cwd": str(project),
                                "objective": "Normal long task checkpoint",
                                "acceptance_criteria": "Do the requested reliability pass.",
                                "current_step": "Checkpoint created from explicit MCP call.",
                                "next_action": "Run the reliability test suite.",
                                "confidence": "medium",
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "compaction_packet", "arguments": {"cwd": str(project)}},
                    },
                ],
            )
            packet = responses[1]["result"]["content"][0]["text"]
            self.assertIn("Normal long task checkpoint", packet)
            self.assertIn("Do the requested reliability pass", packet)
            self.assertIn("Run the reliability test suite", packet)

    def test_mcp_streams_isolate_active_objectives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            responses = self.run_mcp(
                home,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_checkpoint",
                            "arguments": {
                                "cwd": str(project),
                                "stream_id": "agent-a",
                                "stream_label": "Agent A",
                                "objective": "MCP Agent A objective",
                                "next_action": "Do A next",
                                "files_touched": "src/shared.py",
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_checkpoint",
                            "arguments": {
                                "cwd": str(project),
                                "stream_id": "agent-b",
                                "stream_label": "Agent B",
                                "objective": "MCP Agent B objective",
                                "next_action": "Do B next",
                                "files_touched": "src/shared.py",
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_packet",
                            "arguments": {"cwd": str(project), "stream_id": "agent-a"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "compaction_status",
                            "arguments": {"cwd": str(project), "stream_id": "agent-a"},
                        },
                    },
                ],
            )
            packet = responses[2]["result"]["content"][0]["text"]
            status = responses[3]["result"]["content"][0]["text"]
            self.assertIn("MCP Agent A objective", packet)
            self.assertIn("Do A next", packet)
            self.assertIn("MCP Agent B objective", packet)
            self.assertIn("awareness=\"only\"", packet)
            self.assertNotIn("<next_action>\nDo B next", packet)
            self.assertIn('"stream_id": "agent-a"', status)
            self.assertIn('"compaction_epoch": 0', status)
            self.assertIn('"quarantine_count": 0', status)
            self.assertIn("peer_conflicts", status)


if __name__ == "__main__":
    unittest.main()
