from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HOOK_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
}


class PluginShapeTests(unittest.TestCase):
    def test_plugin_manifest_paths_exist_and_are_relative(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        path_keys = {
            "skills": "directory",
            "hooks": "file",
            "mcpServers": "file",
        }
        for key, kind in path_keys.items():
            with self.subTest(key=key):
                raw = manifest[key]
                self.assertIsInstance(raw, str)
                self.assertTrue(raw.startswith("./"))
                target = ROOT / raw[2:]
                if kind == "directory":
                    self.assertTrue(target.is_dir(), target)
                else:
                    self.assertTrue(target.is_file(), target)

    def test_mcp_json_supports_wrapped_compaction_server(self) -> None:
        data = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        self.assertIn("mcp_servers", data)
        server = data["mcp_servers"].get("compaction_sentinel")
        self.assertIsInstance(server, dict)
        self.assertEqual(server.get("command"), "python3")
        self.assertIsInstance(server.get("args"), list)
        self.assertIn("${PLUGIN_ROOT}/plugin_entry.py", server["args"])
        self.assertIn("mcp", server["args"])

    def test_hooks_json_references_expected_events(self) -> None:
        data = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        hooks = data.get("hooks")
        self.assertIsInstance(hooks, dict)
        self.assertEqual(EXPECTED_HOOK_EVENTS, set(hooks))
        for event_name in EXPECTED_HOOK_EVENTS:
            with self.subTest(event_name=event_name):
                groups = hooks[event_name]
                self.assertTrue(groups)
                text = json.dumps(groups)
                self.assertIn("compaction_sentinel.cli hook " + event_name, text)
                if event_name in {"PreToolUse", "PermissionRequest", "PostToolUse"}:
                    self.assertIn('"matcher": "*"', text)


if __name__ == "__main__":
    unittest.main()
