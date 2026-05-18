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


if __name__ == "__main__":
    unittest.main()
