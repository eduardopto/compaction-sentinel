from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from compaction_sentinel.install import doctor, install, remove_toml_table


class InstallTests(unittest.TestCase):
    def test_install_merges_hooks_and_mcp(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            home.mkdir()
            (home / "config.toml").write_text("[features]\nmemories = true\n", encoding="utf-8")
            (home / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {"hooks": [{"type": "command", "command": "python3 existing.py"}]}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            install(source_root=source_root, codex_home=home, skills_target="codex")
            hooks = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
            commands = json.dumps(hooks)
            self.assertIn("existing.py", commands)
            self.assertIn("compaction-sentinel", commands)
            self.assertIn("PermissionRequest", commands)
            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("[mcp_servers.compaction_sentinel]", config)
            self.assertIn("hooks = true", config)
            backups = list((home / "backups" / "compaction-sentinel").glob("*"))
            self.assertTrue(any("hooks.json" in backup.name for backup in backups))
            self.assertTrue(any("config.toml" in backup.name for backup in backups))
            status = doctor(codex_home=home)
            self.assertTrue(status["ok"])
            self.assertTrue(status["runtime_exists"])
            self.assertTrue(status["hooks_present"])
            self.assertTrue(status["hooks_by_event"]["PermissionRequest"])
            self.assertEqual(status["auto_continue"], "off")

    def test_remove_toml_table(self) -> None:
        text = "[features]\nhooks = true\n[mcp_servers.compaction_sentinel]\ncommand = \"x\"\n[other]\na = 1\n"
        cleaned = remove_toml_table(text, "mcp_servers.compaction_sentinel")
        self.assertIn("[features]", cleaned)
        self.assertIn("[other]", cleaned)
        self.assertNotIn("compaction_sentinel", cleaned)

    def test_install_does_not_clobber_existing_public_cs(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            public_bin = home / "bin"
            public_bin.mkdir(parents=True)
            existing = public_bin / "cs"
            existing.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")
            install(source_root=source_root, codex_home=home, skills_target="codex")
            self.assertEqual(existing.read_text(encoding="utf-8"), "#!/bin/sh\necho existing\n")
            self.assertTrue((public_bin / "compaction-sentinel").is_symlink())
            self.assertFalse(doctor(codex_home=home)["public_cli_owned"])

    def test_enable_stop_continue_sets_gentle_policy(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            install(
                source_root=source_root,
                codex_home=home,
                skills_target="codex",
                enable_stop_continue=True,
            )
            self.assertEqual(doctor(codex_home=home)["auto_continue"], "gentle")

    def test_doctor_parses_hooks_feature_as_toml(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            install(source_root=source_root, codex_home=home, skills_target="codex")

            def write_config(hooks_line: str) -> None:
                (home / "config.toml").write_text(
                    "[features]\n"
                    f"{hooks_line}\n\n"
                    "[mcp_servers.other]\ncommand = \"other\"\n\n"
                    "[mcp_servers.compaction_sentinel]\ncommand = \"sentinel\"\nargs = []\n",
                    encoding="utf-8",
                )

            write_config("hooks=true")
            self.assertTrue(doctor(codex_home=home)["hooks_feature_present"])
            write_config("hooks = true")
            self.assertTrue(doctor(codex_home=home)["hooks_feature_present"])
            write_config("hooks = false")
            status = doctor(codex_home=home)
            self.assertFalse(status["hooks_feature_present"])
            self.assertIn("Codex hooks feature is not enabled in config.toml", status["issues"])
            write_config("# hooks = true")
            status = doctor(codex_home=home)
            self.assertFalse(status["hooks_feature_present"])

    def test_doctor_detects_existing_mcp_table_via_toml(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            install(source_root=source_root, codex_home=home, skills_target="codex")
            (home / "config.toml").write_text(
                "[features]\nhooks = true\n\n"
                "[mcp_servers.other]\ncommand = \"other\"\n\n"
                "# [mcp_servers.compaction_sentinel]\n# command = \"commented\"\n",
                encoding="utf-8",
            )
            status = doctor(codex_home=home)
            self.assertFalse(status["mcp_present"])
            self.assertIn("compaction_sentinel MCP config is missing", status["issues"])
            (home / "config.toml").write_text(
                "[features]\nhooks = true\n\n"
                "[mcp_servers.other]\ncommand = \"other\"\n\n"
                "[mcp_servers.compaction_sentinel]\ncommand = \"sentinel\"\nargs = []\n",
                encoding="utf-8",
            )
            self.assertTrue(doctor(codex_home=home)["mcp_present"])

    def test_backup_list_restore_and_purge_uninstall(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            home.mkdir()
            (home / "hooks.json").write_text('{"hooks":{}}\n', encoding="utf-8")
            (home / "config.toml").write_text("[features]\nhooks = false\n", encoding="utf-8")
            install(source_root=source_root, codex_home=home, skills_target="codex")
            from compaction_sentinel.install import list_backups, restore_backup, uninstall

            backups = list_backups(home)
            self.assertTrue(backups)
            hooks_backup = next(item for item in backups if item["target"] == "hooks.json")
            (home / "hooks.json").write_text('{"hooks":{}}\n', encoding="utf-8")
            restored = restore_backup(home, hooks_backup["id"])
            self.assertTrue(Path(restored["target"]).exists())
            result = uninstall(codex_home=home, purge=True)
            self.assertIn("runtime-and-ledger", result["removed"])
            self.assertFalse((home / "compaction-sentinel").exists())


if __name__ == "__main__":
    unittest.main()
