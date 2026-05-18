"""Installer for Codex Desktop integration."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .core import VERSION, install_root, load_runtime_config, write_runtime_config


HOOK_MARKER = "compaction-sentinel"


def hook_command(codex_home: Path, event_name: str) -> str:
    script = install_root(codex_home) / "bin" / "compaction-sentinel"
    return f'{sys.executable} "{script}" --codex-home "{codex_home}" hook {event_name}'


def source_repo_root(source_root: Path) -> Path:
    root = source_root.resolve()
    if (root / "src" / "compaction_sentinel").exists():
        return root
    for parent in root.parents:
        if (parent / "src" / "compaction_sentinel").exists():
            return parent
    return root


def install(
    *,
    source_root: Path,
    codex_home: Path,
    enable_stop_continue: bool = False,
    skills_target: str = "both",
    dry_run: bool = False,
) -> dict[str, Any]:
    source_root = source_repo_root(source_root)
    actions: list[str] = []
    if not dry_run:
        install_package(source_root, codex_home)
        install_skill(source_root, codex_home, skills_target)
        merge_hooks(codex_home)
        merge_mcp_config(codex_home)
        ensure_hooks_feature(codex_home)
        config = load_runtime_config(codex_home)
        config["auto_continue"] = "gentle" if enable_stop_continue else str(config.get("auto_continue") or "off")
        write_runtime_config(config, codex_home)
    actions.extend(
        [
            f"runtime -> {install_root(codex_home)}",
            f"hooks -> {codex_home / 'hooks.json'}",
            f"mcp -> {codex_home / 'config.toml'}",
            f"skills_target -> {skills_target}",
            f"auto_continue -> {'gentle' if enable_stop_continue else 'off'}",
        ]
    )
    return {"version": VERSION, "dry_run": dry_run, "actions": actions}


def install_package(source_root: Path, codex_home: Path) -> None:
    root = install_root(codex_home)
    package_dst = root / "runtime" / "compaction_sentinel"
    if package_dst.exists():
        shutil.rmtree(package_dst)
    package_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root / "src" / "compaction_sentinel", package_dst)

    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / "compaction-sentinel"
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import runpy\n"
        "import sys\n"
        f"sys.path.insert(0, {str(root / 'runtime')!r})\n"
        "runpy.run_module('compaction_sentinel.cli', run_name='__main__')\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    cs = bin_dir / "cs"
    if cs.exists() or cs.is_symlink():
        cs.unlink()
    cs.symlink_to(launcher)

    public_bin = codex_home / "bin"
    public_bin.mkdir(parents=True, exist_ok=True)
    for name in ("compaction-sentinel", "cs"):
        public_link = public_bin / name
        install_public_link(public_link, launcher)


def install_public_link(link: Path, target: Path) -> None:
    if link.is_symlink():
        try:
            existing = link.resolve(strict=False)
        except Exception:
            existing = None
        if existing == target.resolve(strict=False) or "compaction-sentinel" in str(existing):
            link.unlink()
            link.symlink_to(target)
        return
    if link.exists():
        return
    link.symlink_to(target)


def install_skill(source_root: Path, codex_home: Path, target: str) -> None:
    skill_src = source_root / "skills" / "compaction-sentinel"
    targets: list[Path] = []
    if target in {"codex", "both"}:
        targets.append(codex_home / "skills" / "compaction-sentinel")
    if target in {"agents", "both"}:
        targets.append(Path.home() / ".agents" / "skills" / "compaction-sentinel")
    for dst in targets:
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_src, dst)


def sentinel_hook_group(codex_home: Path, event_name: str, timeout: int) -> dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": hook_command(codex_home, event_name),
                "timeout": timeout,
                "statusMessage": f"{HOOK_MARKER}: {event_name}",
            }
        ]
    }


def merge_hooks(codex_home: Path) -> None:
    hooks_path = codex_home / "hooks.json"
    data: dict[str, Any] = {"hooks": {}}
    if hooks_path.exists():
        try:
            loaded = json.loads(hooks_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            backup = hooks_path.with_suffix(".json.bak")
            shutil.copy2(hooks_path, backup)
    hooks = data.setdefault("hooks", {})
    events = {
        "SessionStart": 10,
        "UserPromptSubmit": 10,
        "PreToolUse": 5,
        "PostToolUse": 5,
        "Stop": 10,
    }
    for event_name, timeout in events.items():
        groups = hooks.setdefault(event_name, [])
        groups[:] = [group for group in groups if not hook_group_has_marker(group)]
        group = sentinel_hook_group(codex_home, event_name, timeout)
        if event_name in {"PreToolUse", "PostToolUse"}:
            group["matcher"] = "*"
        groups.append(group)
    codex_home.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def hook_group_has_marker(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks", []):
        if isinstance(hook, dict):
            command = str(hook.get("command") or "")
            status = str(hook.get("statusMessage") or "")
            if HOOK_MARKER in command or HOOK_MARKER in status:
                return True
    return False


def merge_mcp_config(codex_home: Path) -> None:
    config = codex_home / "config.toml"
    existing = config.read_text(encoding="utf-8") if config.exists() else ""
    block = (
        "\n[mcp_servers.compaction_sentinel]\n"
        f'command = "{sys.executable}"\n'
        f'args = ["{install_root(codex_home) / "bin" / "compaction-sentinel"}", "--codex-home", "{codex_home}", "mcp"]\n'
    )
    cleaned = remove_toml_table(existing, "mcp_servers.compaction_sentinel").rstrip()
    config.write_text(cleaned + "\n" + block + "\n", encoding="utf-8")


def ensure_hooks_feature(codex_home: Path) -> None:
    config = codex_home / "config.toml"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if "[features]" not in text:
        config.write_text(text.rstrip() + "\n\n[features]\nhooks = true\n", encoding="utf-8")
        return
    lines = text.splitlines()
    out: list[str] = []
    in_features = False
    saw_hooks = False
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_features and not saw_hooks:
                out.append("hooks = true")
                inserted = True
            in_features = stripped == "[features]"
        if in_features and stripped.startswith("hooks"):
            out.append("hooks = true")
            saw_hooks = True
            continue
        out.append(line)
    if in_features and not saw_hooks:
        out.append("hooks = true")
        inserted = True
    config.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def remove_toml_table(text: str, table: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    skip = False
    header = f"[{table}]"
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            skip = True
            continue
        if skip and stripped.startswith("[") and stripped.endswith("]"):
            skip = False
        if not skip:
            out.append(line)
    return "\n".join(out)


def uninstall(*, codex_home: Path) -> dict[str, Any]:
    hooks_path = codex_home / "hooks.json"
    removed: list[str] = []
    if hooks_path.exists():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict):
            hooks = data.get("hooks")
            if isinstance(hooks, dict):
                for event_name, groups in list(hooks.items()):
                    if isinstance(groups, list):
                        new_groups = [group for group in groups if not hook_group_has_marker(group)]
                        if len(new_groups) != len(groups):
                            hooks[event_name] = new_groups
                            removed.append(f"hook:{event_name}")
                hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    config = codex_home / "config.toml"
    if config.exists():
        config.write_text(
            remove_toml_table(config.read_text(encoding="utf-8"), "mcp_servers.compaction_sentinel").rstrip()
            + "\n",
            encoding="utf-8",
        )
        removed.append("mcp:compaction_sentinel")
    return {"version": VERSION, "removed": removed, "runtime_left_in_place": str(install_root(codex_home))}


def doctor(*, codex_home: Path) -> dict[str, Any]:
    root = install_root(codex_home)
    hooks_path = codex_home / "hooks.json"
    config_path = codex_home / "config.toml"
    hooks_present = False
    if hooks_path.exists():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            hooks_present = HOOK_MARKER in json.dumps(data)
        except Exception:
            hooks_present = False
    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    return {
        "version": VERSION,
        "codex_home": str(codex_home),
        "platform": os.uname().sysname if hasattr(os, "uname") else sys.platform,
        "runtime": str(root),
        "runtime_exists": root.exists(),
        "public_cli": str(codex_home / "bin" / "cs"),
        "public_cli_exists": (codex_home / "bin" / "cs").exists(),
        "hooks_json": str(hooks_path),
        "hooks_present": hooks_present,
        "mcp_present": "[mcp_servers.compaction_sentinel]" in config_text,
        "hooks_feature_present": "hooks = true" in config_text,
    }
