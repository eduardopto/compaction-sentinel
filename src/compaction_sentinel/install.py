"""Installer for Codex Desktop integration."""

from __future__ import annotations

import json
import platform
import shutil
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from .core import VERSION, install_root, load_runtime_config, write_runtime_config


HOOK_MARKER = "compaction-sentinel"
SUPPORTED_MACOS = "Darwin"
MIN_PYTHON = (3, 11)
HOOK_EVENTS = {
    "SessionStart": 10,
    "UserPromptSubmit": 10,
    "PreToolUse": 5,
    "PermissionRequest": 5,
    "PostToolUse": 5,
    "Stop": 10,
}


def hook_command(codex_home: Path, event_name: str) -> str:
    script = install_root(codex_home) / "bin" / "compaction-sentinel"
    return " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            "--codex-home",
            shlex.quote(str(codex_home)),
            "hook",
            shlex.quote(event_name),
        ]
    )


def source_repo_root(source_root: Path) -> Path:
    root = source_root.resolve()
    if (root / "src" / "compaction_sentinel").exists():
        return root
    for parent in root.parents:
        if (parent / "src" / "compaction_sentinel").exists():
            return parent
    return root


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def runtime_source(source_root: Path) -> Path:
    source_root = source_repo_root(source_root)
    repo_runtime = source_root / "src" / "compaction_sentinel"
    if repo_runtime.exists():
        return repo_runtime
    return package_dir()


def skill_source(source_root: Path) -> Path:
    source_root = source_repo_root(source_root)
    repo_skill = source_root / "skills" / "compaction-sentinel"
    if repo_skill.exists():
        return repo_skill
    packaged_skill = package_dir() / "assets" / "skills" / "compaction-sentinel"
    if packaged_skill.exists():
        return packaged_skill
    raise FileNotFoundError("Compaction Sentinel skill assets are missing.")


def ensure_source_assets(source_root: Path) -> None:
    runtime = runtime_source(source_root)
    skill = skill_source(source_root)
    missing = [str(path) for path in (runtime, skill / "SKILL.md") if not path.exists()]
    if missing:
        raise FileNotFoundError("Install assets are missing: " + ", ".join(missing))


def install(
    *,
    source_root: Path,
    codex_home: Path,
    enable_stop_continue: bool = False,
    auto_continue: str | None = None,
    skills_target: str = "both",
    dry_run: bool = False,
) -> dict[str, Any]:
    ensure_supported_environment()
    source_root = source_repo_root(source_root)
    ensure_source_assets(source_root)
    actions: list[str] = []
    actual_auto_continue = auto_continue or ("gentle" if enable_stop_continue else None)
    if not dry_run:
        install_package(source_root, codex_home)
        install_skill(source_root, codex_home, skills_target)
        merge_hooks(codex_home)
        merge_mcp_config(codex_home)
        ensure_hooks_feature(codex_home)
        config = load_runtime_config(codex_home)
        if actual_auto_continue is not None:
            config["auto_continue"] = actual_auto_continue
        actual_auto_continue = str(config.get("auto_continue") or "off")
        write_runtime_config(config, codex_home)
    else:
        actual_auto_continue = actual_auto_continue or str(load_runtime_config(codex_home).get("auto_continue") or "off")
    actions.extend(
        [
            f"runtime -> {install_root(codex_home)}",
            f"hooks -> {codex_home / 'hooks.json'}",
            f"mcp -> {codex_home / 'config.toml'}",
            f"skills_target -> {skills_target}",
            f"auto_continue -> {actual_auto_continue}",
        ]
    )
    return {"version": VERSION, "platform": platform.system(), "dry_run": dry_run, "actions": actions}


def ensure_supported_environment() -> None:
    if platform.system() != SUPPORTED_MACOS:
        raise RuntimeError("Compaction Sentinel currently supports Codex Desktop on macOS only.")
    if sys.version_info < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        raise RuntimeError(f"Python {required}+ is required.")


def install_package(source_root: Path, codex_home: Path) -> None:
    root = install_root(codex_home)
    package_dst = root / "runtime" / "compaction_sentinel"
    if package_dst.exists():
        shutil.rmtree(package_dst)
    package_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        runtime_source(source_root),
        package_dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.sqlite", "*.log"),
    )

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
    skill_src = skill_source(source_root)
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
    group: dict[str, Any] = {
        "hooks": [
            {
                "type": "command",
                "command": hook_command(codex_home, event_name),
                "timeout": timeout,
                "statusMessage": f"{HOOK_MARKER}: {event_name}",
            }
        ]
    }
    if event_name in {"PreToolUse", "PermissionRequest", "PostToolUse"}:
        group["matcher"] = "*"
    return group


def merge_hooks(codex_home: Path) -> None:
    hooks_path = codex_home / "hooks.json"
    backup_file(hooks_path)
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
    for event_name, timeout in HOOK_EVENTS.items():
        groups = hooks.setdefault(event_name, [])
        groups[:] = [group for group in groups if not hook_group_has_marker(group)]
        groups.append(sentinel_hook_group(codex_home, event_name, timeout))
    codex_home.mkdir(parents=True, exist_ok=True)
    atomic_write_text(hooks_path, json.dumps(data, indent=2) + "\n")


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
    backup_file(config)
    existing = config.read_text(encoding="utf-8") if config.exists() else ""
    script = install_root(codex_home) / "bin" / "compaction-sentinel"
    block = (
        "\n[mcp_servers.compaction_sentinel]\n"
        f"command = {toml_string(sys.executable)}\n"
        f"args = [{toml_string(str(script))}, \"--codex-home\", {toml_string(str(codex_home))}, \"mcp\"]\n"
    )
    cleaned = remove_toml_table(existing, "mcp_servers.compaction_sentinel").rstrip()
    atomic_write_text(config, cleaned + "\n" + block + "\n")


def ensure_hooks_feature(codex_home: Path) -> None:
    config = codex_home / "config.toml"
    backup_file(config)
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if "[features]" not in text:
        atomic_write_text(config, text.rstrip() + "\n\n[features]\nhooks = true\n")
        return
    lines = text.splitlines()
    out: list[str] = []
    in_features = False
    saw_hooks = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_features and not saw_hooks:
                out.append("hooks = true")
            in_features = stripped == "[features]"
        if in_features and stripped.startswith("hooks"):
            out.append("hooks = true")
            saw_hooks = True
            continue
        out.append(line)
    if in_features and not saw_hooks:
        out.append("hooks = true")
    atomic_write_text(config, "\n".join(out).rstrip() + "\n")


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir = path.parent / "backups" / "compaction-sentinel"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{path.name}.{stamp}.bak"
    counter = 1
    while backup.exists():
        backup = backup_dir / f"{path.name}.{stamp}.{counter}.bak"
        counter += 1
    shutil.copy2(path, backup)
    return backup


def list_backups(codex_home: Path) -> list[dict[str, str]]:
    backup_dir = codex_home / "backups" / "compaction-sentinel"
    if not backup_dir.exists():
        return []
    out: list[dict[str, str]] = []
    for path in sorted(backup_dir.glob("*.bak"), key=lambda item: item.stat().st_mtime, reverse=True):
        out.append(
            {
                "id": path.name,
                "path": str(path),
                "target": backup_target_name(path.name),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime)),
            }
        )
    return out


def backup_target_name(name: str) -> str:
    for target in ("hooks.json", "config.toml"):
        if name.startswith(target + "."):
            return target
    return name.split(".")[0]


def restore_backup(codex_home: Path, backup_id: str) -> dict[str, str]:
    backup_dir = codex_home / "backups" / "compaction-sentinel"
    backup = backup_dir / backup_id
    if not backup.exists():
        matches = list(backup_dir.glob(f"*{backup_id}*")) if backup_dir.exists() else []
        if len(matches) == 1:
            backup = matches[0]
        else:
            raise FileNotFoundError(f"backup not found or ambiguous: {backup_id}")
    target_name = backup_target_name(backup.name)
    target = codex_home / target_name
    backup_file(target)
    shutil.copy2(backup, target)
    return {"restored": str(backup), "target": str(target)}


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def toml_string(value: str) -> str:
    return json.dumps(value)


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


def uninstall(*, codex_home: Path, purge: bool = False) -> dict[str, Any]:
    hooks_path = codex_home / "hooks.json"
    removed: list[str] = []
    if hooks_path.exists():
        backup_file(hooks_path)
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
                atomic_write_text(hooks_path, json.dumps(data, indent=2) + "\n")
    config = codex_home / "config.toml"
    if config.exists():
        backup_file(config)
        atomic_write_text(
            config,
            remove_toml_table(config.read_text(encoding="utf-8"), "mcp_servers.compaction_sentinel").rstrip()
            + "\n",
        )
        removed.append("mcp:compaction_sentinel")
    for link in (codex_home / "bin" / "compaction-sentinel", codex_home / "bin" / "cs"):
        if public_link_points_to_sentinel(link):
            link.unlink()
            removed.append(f"cli:{link.name}")
    if purge and install_root(codex_home).exists():
        shutil.rmtree(install_root(codex_home))
        removed.append("runtime-and-ledger")
    return {
        "version": VERSION,
        "removed": removed,
        "runtime_left_in_place": None if purge else str(install_root(codex_home)),
    }


def doctor(*, codex_home: Path) -> dict[str, Any]:
    root = install_root(codex_home)
    hooks_path = codex_home / "hooks.json"
    config_path = codex_home / "config.toml"
    issues: list[str] = []
    warnings: list[str] = []
    hooks_by_event: dict[str, bool] = {event_name: False for event_name in HOOK_EVENTS}
    if hooks_path.exists():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            for event_name in HOOK_EVENTS:
                hooks_by_event[event_name] = any(
                    hook_group_has_marker(group)
                    for group in data.get("hooks", {}).get(event_name, [])
                )
        except Exception:
            issues.append("hooks.json is not valid JSON")
    else:
        issues.append("hooks.json is missing")
    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    mcp_present = "[mcp_servers.compaction_sentinel]" in config_text
    hooks_feature_present = "hooks = true" in config_text
    public_cli = codex_home / "bin" / "cs"
    public_cli_owned = public_link_points_to_sentinel(public_cli)
    if platform.system() != SUPPORTED_MACOS:
        issues.append("host is not macOS")
    if sys.version_info < MIN_PYTHON:
        issues.append("python is older than 3.11")
    if not root.exists():
        issues.append("runtime is missing")
    if not all(hooks_by_event.values()):
        issues.append("one or more Compaction Sentinel hook entries are missing")
    if not mcp_present:
        issues.append("compaction_sentinel MCP config is missing")
    if not hooks_feature_present:
        issues.append("Codex hooks feature is not enabled in config.toml")
    if public_cli.exists() and not public_cli_owned:
        warnings.append("~/.codex/bin/cs exists but is not owned by Compaction Sentinel; use ~/.codex/compaction-sentinel/bin/cs")
    config = load_runtime_config(codex_home)
    return {
        "version": VERSION,
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "codex_home": str(codex_home),
        "platform": platform.system(),
        "python": sys.version.split()[0],
        "runtime": str(root),
        "runtime_exists": root.exists(),
        "internal_cli": str(root / "bin" / "cs"),
        "internal_cli_exists": (root / "bin" / "cs").exists(),
        "public_cli": str(public_cli),
        "public_cli_exists": public_cli.exists(),
        "public_cli_owned": public_cli_owned,
        "hooks_json": str(hooks_path),
        "hooks_by_event": hooks_by_event,
        "hooks_present": all(hooks_by_event.values()),
        "mcp_present": mcp_present,
        "hooks_feature_present": hooks_feature_present,
        "auto_continue": config.get("auto_continue"),
        "retention_days": config.get("retention_days"),
        "redact": config.get("redact"),
        "backups": len(list_backups(codex_home)),
    }


def doctor_fix(*, codex_home: Path) -> dict[str, Any]:
    actions: list[str] = []
    root = install_root(codex_home)
    if not root.exists():
        raise RuntimeError("runtime is missing; run `compaction-sentinel install` from a checkout or pipx/uvx install first")
    merge_hooks(codex_home)
    actions.append("merged hooks.json")
    merge_mcp_config(codex_home)
    actions.append("merged MCP config")
    ensure_hooks_feature(codex_home)
    actions.append("enabled hooks feature")
    launcher = root / "bin" / "compaction-sentinel"
    public_bin = codex_home / "bin"
    public_bin.mkdir(parents=True, exist_ok=True)
    install_public_link(public_bin / "compaction-sentinel", launcher)
    install_public_link(public_bin / "cs", launcher)
    actions.append("checked CLI links")
    result = doctor(codex_home=codex_home)
    result["fix_actions"] = actions
    return result


def doctor_explanations(result: dict[str, Any]) -> list[str]:
    explanations: list[str] = []
    for issue in result.get("issues", []):
        text = str(issue)
        if "hooks.json" in text:
            explanations.append("Hooks load the automatic resume packet; rerun install or `cs doctor --fix`.")
        elif "MCP" in text or "mcp" in text:
            explanations.append("MCP tools let Codex explicitly save/search checkpoints; `cs doctor --fix` rewrites the config block.")
        elif "runtime" in text:
            explanations.append("The runtime contains the hook scripts and local ledger code; reinstall from the package.")
        elif "hooks feature" in text:
            explanations.append("Codex must have `[features] hooks = true` for user-level hooks to run.")
        else:
            explanations.append(text)
    if not explanations:
        explanations.append("No blocking install issues detected.")
    return explanations


def public_link_points_to_sentinel(link: Path) -> bool:
    if not link.is_symlink():
        return False
    try:
        target = link.resolve(strict=False)
    except Exception:
        return False
    return "compaction-sentinel" in str(target)
