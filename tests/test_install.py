from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compaction_sentinel.install import doctor, doctor_explanations, doctor_fix, install, remove_toml_table, uninstall


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
            launcher = (home / "compaction-sentinel" / "bin" / "compaction-sentinel").read_text(encoding="utf-8")
            self.assertTrue(launcher.startswith(f"#!{sys.executable}\n"))

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

    def test_doctor_warns_when_plain_cs_is_not_on_path(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            install(source_root=source_root, codex_home=home, skills_target="codex")
            with patch.dict("os.environ", {"PATH": ""}):
                status = doctor(codex_home=home)
            self.assertTrue(status["ok"])
            self.assertIsNone(status["path_cli"])
            self.assertTrue(status["public_cli_owned"])
            self.assertTrue(any("plain `cs` is not discoverable" in item for item in status["warnings"]))
            explanations = doctor_explanations(status)
            self.assertTrue(any("--global-bin /opt/homebrew/bin" in item for item in explanations))

    def test_doctor_warns_when_plain_cs_points_elsewhere(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            other_bin = Path(tmp) / "other-bin"
            other_bin.mkdir()
            other_cs = other_bin / "cs"
            other_cs.write_text("#!/bin/sh\necho not-sentinel\n", encoding="utf-8")
            other_cs.chmod(0o755)
            install(source_root=source_root, codex_home=home, skills_target="codex")
            with patch.dict("os.environ", {"PATH": str(other_bin)}):
                status = doctor(codex_home=home)
            self.assertEqual(status["path_cli"], str(other_cs))
            self.assertFalse(status["path_cli_owned"])
            self.assertTrue(any("plain `cs` resolves to non-Sentinel" in item for item in status["warnings"]))

    def test_global_bin_is_opt_in_recorded_and_uninstalled(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            global_bin = Path(tmp) / "global-bin"
            install(source_root=source_root, codex_home=home, skills_target="codex", global_bin=global_bin)
            self.assertTrue((global_bin / "cs").is_symlink())
            self.assertTrue((global_bin / "compaction-sentinel").is_symlink())
            with patch.dict("os.environ", {"PATH": str(global_bin)}):
                status = doctor(codex_home=home)
            self.assertEqual(status["path_cli"], str(global_bin / "cs"))
            self.assertTrue(status["path_cli_owned"])
            self.assertIn(str(global_bin), status["global_shim_bins"])
            self.assertFalse(any("plain `cs` is not discoverable" in item for item in status["warnings"]))
            result = uninstall(codex_home=home, purge=True)
            self.assertTrue(any(item.startswith("global-cli:") for item in result["removed"]))
            self.assertFalse((global_bin / "cs").exists())
            self.assertFalse((global_bin / "compaction-sentinel").exists())

    def test_global_bin_conflict_fails_clearly(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            global_bin = Path(tmp) / "global-bin"
            global_bin.mkdir()
            conflict = global_bin / "cs"
            conflict.write_text("#!/bin/sh\necho other\n", encoding="utf-8")
            conflict.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "global shim path exists"):
                install(source_root=source_root, codex_home=home, skills_target="codex", global_bin=global_bin)
            self.assertFalse((home / "compaction-sentinel").exists())

    def test_doctor_fix_repairs_recorded_global_bin(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            global_bin = Path(tmp) / "global-bin"
            install(source_root=source_root, codex_home=home, skills_target="codex", global_bin=global_bin)
            (global_bin / "cs").unlink()
            status = doctor(codex_home=home)
            self.assertTrue(any("recorded global Sentinel shim" in item for item in status["warnings"]))
            fixed = doctor_fix(codex_home=home)
            self.assertTrue((global_bin / "cs").is_symlink())
            self.assertIn(f"checked global CLI links in {global_bin}", fixed["fix_actions"])

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

    def test_skill_cli_fallback_uses_guaranteed_codex_bin_path(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for skill in (
            root / "skills" / "compaction-sentinel" / "SKILL.md",
            root / "src" / "compaction_sentinel" / "assets" / "skills" / "compaction-sentinel" / "SKILL.md",
        ):
            text = skill.read_text(encoding="utf-8")
            self.assertIn("~/.codex/bin/cs checkpoint", text)
            self.assertNotIn("\ncs checkpoint", text)


if __name__ == "__main__":
    unittest.main()
