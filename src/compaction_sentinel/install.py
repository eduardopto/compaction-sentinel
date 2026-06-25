"""Installer for Codex Desktop integration."""

from __future__ import annotations

import json
import platform
import re
import shutil
import shlex
import sqlite3
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from .core import VERSION, db_path, install_root, load_runtime_config, write_runtime_config


HOOK_MARKER = "compaction-sentinel"
SKILL_NAME = "compaction-sentinel"
SUPPORTED_MACOS = "Darwin"
MIN_PYTHON = (3, 11)
HOOK_EVENTS = {
    "SessionStart": 10,
    "UserPromptSubmit": 10,
    "PreCompact": 10,
    "PostCompact": 10,
    "PreToolUse": 5,
    "PermissionRequest": 5,
    "PostToolUse": 5,
    "Stop": 10,
}
NON_HOT_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "PermissionRequest", "Stop")
HOOK_PROFILES = {
    "full": tuple(HOOK_EVENTS),
    "balanced": tuple(HOOK_EVENTS),
    "light": NON_HOT_HOOK_EVENTS,
}
CODEX_CONTEXT_MARKERS = ("codex-context", "codex_context.py")


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
    hooks_profile: str = "balanced",
    global_bin: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ensure_supported_environment()
    source_root = source_repo_root(source_root)
    ensure_source_assets(source_root)
    actions: list[str] = []
    actual_auto_continue = auto_continue or ("gentle" if enable_stop_continue else None)
    normalized_hooks_profile = normalize_hooks_profile(hooks_profile)
    if global_bin is not None:
        global_bin = normalize_bin_dir(global_bin)
        validate_global_bin_available(global_bin)
    if not dry_run:
        install_package(source_root, codex_home)
        install_skill(source_root, codex_home, skills_target)
        merge_hooks(codex_home, hooks_profile=normalized_hooks_profile)
        merge_mcp_config(codex_home)
        ensure_hooks_feature(codex_home)
        config = load_runtime_config(codex_home)
        if global_bin is not None:
            install_global_links(global_bin, launcher_path(codex_home))
            remember_global_bin(config, global_bin)
        config["skills_target"] = normalize_skills_target(skills_target)
        config["hooks_profile"] = normalized_hooks_profile
        config["performance_mode"] = normalized_hooks_profile
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
            f"hooks_profile -> {normalized_hooks_profile}",
            f"auto_continue -> {actual_auto_continue}",
        ]
    )
    if global_bin is not None:
        actions.append(f"global_bin -> {normalize_bin_dir(global_bin)}")
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
        f"#!{sys.executable}\n"
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


def launcher_path(codex_home: Path) -> Path:
    return install_root(codex_home) / "bin" / "compaction-sentinel"


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


def normalize_bin_dir(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else expanded.resolve(strict=False)


def install_global_links(bin_dir: Path, launcher: Path) -> list[Path]:
    bin_dir = normalize_bin_dir(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    links = [bin_dir / "compaction-sentinel", bin_dir / "cs"]
    for link in links:
        install_public_link(link, launcher)
    blocked = [str(link) for link in links if not public_link_points_to_sentinel(link)]
    if blocked:
        raise RuntimeError(
            "global shim path exists but is not owned by Compaction Sentinel: "
            + ", ".join(blocked)
        )
    return links


def validate_global_bin_available(bin_dir: Path) -> None:
    for name in ("compaction-sentinel", "cs"):
        link = normalize_bin_dir(bin_dir) / name
        if (link.exists() or link.is_symlink()) and not public_link_points_to_sentinel(link):
            raise RuntimeError(f"global shim path exists but is not owned by Compaction Sentinel: {link}")


def configured_global_bins(config: dict[str, Any]) -> list[Path]:
    raw = config.get("global_shim_bins")
    if not isinstance(raw, list):
        return []
    bins: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        path = normalize_bin_dir(Path(item))
        key = str(path)
        if key not in seen:
            seen.add(key)
            bins.append(path)
    return bins


def remember_global_bin(config: dict[str, Any], bin_dir: Path) -> None:
    bins = {str(path) for path in configured_global_bins(config)}
    bins.add(str(normalize_bin_dir(bin_dir)))
    config["global_shim_bins"] = sorted(bins)


def sentinel_path_owned(path: str | None) -> bool:
    if not path:
        return False
    return public_link_points_to_sentinel(Path(path))


def normalize_skills_target(target: Any) -> str:
    value = str(target or "codex").strip().lower()
    return value if value in {"codex", "agents", "both"} else "codex"


def normalize_hooks_profile(profile: Any) -> str:
    value = str(profile or "balanced").strip().lower()
    return value if value in HOOK_PROFILES else "balanced"


def hook_events_for_profile(profile: Any) -> tuple[str, ...]:
    return HOOK_PROFILES[normalize_hooks_profile(profile)]


def skill_target_paths(codex_home: Path, target: Any) -> dict[str, Path]:
    normalized = normalize_skills_target(target)
    paths: dict[str, Path] = {}
    if normalized in {"codex", "both"}:
        paths["codex"] = codex_home / "skills" / SKILL_NAME
    if normalized in {"agents", "both"}:
        paths["agents"] = Path.home() / ".agents" / "skills" / SKILL_NAME
    return paths


def skill_dir_owned(path: Path) -> bool:
    if path.name != SKILL_NAME:
        return False
    skill_file = path / "SKILL.md"
    if not skill_file.exists():
        return False
    try:
        text = skill_file.read_text(encoding="utf-8")[:1200]
    except Exception:
        return False
    has_name = bool(re.search(r"(?m)^name:\s*compaction-sentinel\s*$", text))
    has_heading = "# Compaction Sentinel" in text
    return has_name and has_heading


def copy_skill(skill_src: Path, dst: Path) -> None:
    if dst.exists():
        if not skill_dir_owned(dst):
            raise RuntimeError(f"skill directory exists but is not owned by Compaction Sentinel: {dst}")
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_src, dst)


def runtime_skill_source(codex_home: Path) -> Path:
    candidate = install_root(codex_home) / "runtime" / "compaction_sentinel" / "assets" / "skills" / SKILL_NAME
    if (candidate / "SKILL.md").exists():
        return candidate
    packaged = package_dir() / "assets" / "skills" / SKILL_NAME
    if (packaged / "SKILL.md").exists():
        return packaged
    raise FileNotFoundError("Compaction Sentinel skill assets are missing from the runtime.")


def install_skill(source_root: Path, codex_home: Path, target: str) -> None:
    skill_src = skill_source(source_root)
    for dst in skill_target_paths(codex_home, target).values():
        copy_skill(skill_src, dst)


def repair_skill_install(codex_home: Path, target: Any) -> list[str]:
    skill_src = runtime_skill_source(codex_home)
    repaired: list[str] = []
    for label, dst in skill_target_paths(codex_home, target).items():
        if skill_dir_owned(dst):
            continue
        if dst.exists() and not skill_dir_owned(dst):
            continue
        copy_skill(skill_src, dst)
        repaired.append(label)
    return repaired


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
    if event_name in {"PreCompact", "PostCompact"}:
        group["matcher"] = "manual|auto"
    return group


def merge_hooks(codex_home: Path, *, hooks_profile: str = "balanced") -> None:
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
    for event_name, groups in list(hooks.items()):
        if isinstance(groups, list):
            hooks[event_name] = [group for group in groups if not hook_group_has_marker(group)]
    for event_name in hook_events_for_profile(hooks_profile):
        timeout = HOOK_EVENTS[event_name]
        groups = hooks.setdefault(event_name, [])
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


def hook_group_has_codex_context(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks", []):
        if isinstance(hook, dict):
            command = str(hook.get("command") or "")
            if any(marker in command for marker in CODEX_CONTEXT_MARKERS):
                return True
    return False


def codex_context_hook_entries(codex_home: Path) -> list[dict[str, Any]]:
    hooks_path = codex_home / "hooks.json"
    if not hooks_path.exists():
        return []
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    hooks = data.get("hooks") if isinstance(data, dict) else {}
    if not isinstance(hooks, dict):
        return []
    entries: list[dict[str, Any]] = []
    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for index, group in enumerate(groups):
            if hook_group_has_codex_context(group):
                entries.append({"event_name": event_name, "index": index, "group": group})
    return entries


def replace_codex_context_hooks(codex_home: Path) -> dict[str, Any]:
    hooks_path = codex_home / "hooks.json"
    if not hooks_path.exists():
        return {"removed": 0, "added": [], "hooks_json": str(hooks_path), "changed": False}
    backup_file(hooks_path)
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"hooks.json is not valid JSON: {exc}") from exc
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    removed_events: list[str] = []
    removed = 0
    for event_name, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if hook_group_has_codex_context(group):
                removed += 1
                removed_events.append(str(event_name))
            else:
                kept.append(group)
        hooks[event_name] = kept
    added: list[str] = []
    for event_name in sorted(set(removed_events)):
        if event_name not in HOOK_EVENTS:
            continue
        groups = hooks.setdefault(event_name, [])
        if not any(hook_group_has_marker(group) for group in groups):
            groups.append(sentinel_hook_group(codex_home, event_name, HOOK_EVENTS[event_name]))
            added.append(event_name)
    atomic_write_text(hooks_path, json.dumps(data, indent=2) + "\n")
    return {"removed": removed, "added": added, "hooks_json": str(hooks_path), "changed": bool(removed or added)}


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
    lines = text.splitlines()
    out: list[str] = []
    in_features = False
    saw_features = False
    saw_hooks = False
    for line in lines:
        stripped = line.strip()
        table_match = re.match(r"^\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$", stripped)
        if table_match:
            if in_features and not saw_hooks:
                out.append("hooks = true")
            table_name = table_match.group(1)
            in_features = table_name == "features"
            if in_features:
                saw_features = True
                saw_hooks = False
        if in_features and re.match(r"^hooks\s*=", stripped):
            out.append("hooks = true")
            saw_hooks = True
            continue
        out.append(line)
    if in_features and not saw_hooks:
        out.append("hooks = true")
    if not saw_features:
        if out and out[-1].strip():
            out.append("")
        out.extend(["[features]", "hooks = true"])
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
    runtime_config = load_runtime_config(codex_home)
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
    for bin_dir in configured_global_bins(runtime_config):
        for link in (bin_dir / "compaction-sentinel", bin_dir / "cs"):
            if public_link_points_to_sentinel(link):
                link.unlink()
                removed.append(f"global-cli:{link}")
    skills_target = runtime_config.get("skills_target") or "codex"
    for label, skill_dir in skill_target_paths(codex_home, skills_target).items():
        if skill_dir_owned(skill_dir):
            shutil.rmtree(skill_dir)
            removed.append(f"skill:{label}")
    if configured_global_bins(runtime_config) and not purge and install_root(codex_home).exists():
        runtime_config["global_shim_bins"] = []
        write_runtime_config(runtime_config, codex_home)
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
    competing_injectors: list[dict[str, Any]] = []
    if hooks_path.exists():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            for event_name in HOOK_EVENTS:
                hooks_by_event[event_name] = any(
                    hook_group_has_marker(group)
                    for group in data.get("hooks", {}).get(event_name, [])
                )
            competing_injectors = codex_context_hook_entries(codex_home)
        except Exception:
            issues.append("hooks.json is not valid JSON")
    else:
        issues.append("hooks.json is missing")
    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    config_data: dict[str, Any] = {}
    if config_text:
        try:
            loaded_config = tomllib.loads(config_text)
            if isinstance(loaded_config, dict):
                config_data = loaded_config
        except tomllib.TOMLDecodeError as exc:
            issues.append(f"config.toml is not valid TOML: {exc}")
    features = config_data.get("features") if isinstance(config_data.get("features"), dict) else {}
    mcp_servers = config_data.get("mcp_servers") if isinstance(config_data.get("mcp_servers"), dict) else {}
    mcp_present = isinstance(mcp_servers, dict) and "compaction_sentinel" in mcp_servers
    hooks_feature_present = features.get("hooks") is True
    public_cli = codex_home / "bin" / "cs"
    public_cli_owned = public_link_points_to_sentinel(public_cli)
    path_cli = shutil.which("cs")
    path_cli_owned = sentinel_path_owned(path_cli)
    config = load_runtime_config(codex_home)
    hooks_profile = normalize_hooks_profile(config.get("hooks_profile") or "balanced")
    expected_hooks = set(hook_events_for_profile(hooks_profile))
    skills_target = normalize_skills_target(config.get("skills_target") or "codex")
    skill_status = {
        label: {
            "path": str(path),
            "exists": path.exists(),
            "owned": skill_dir_owned(path),
        }
        for label, path in skill_target_paths(codex_home, skills_target).items()
    }
    skills_present = all(bool(item["owned"]) for item in skill_status.values()) if skill_status else False
    global_bins = configured_global_bins(config)
    global_cli_links = {
        str(bin_dir / name): public_link_points_to_sentinel(bin_dir / name)
        for bin_dir in global_bins
        for name in ("compaction-sentinel", "cs")
    }
    if platform.system() != SUPPORTED_MACOS:
        issues.append("host is not macOS")
    if sys.version_info < MIN_PYTHON:
        issues.append("python is older than 3.11")
    if not root.exists():
        issues.append("runtime is missing")
    missing_expected_hooks = [event_name for event_name in hook_events_for_profile(hooks_profile) if not hooks_by_event[event_name]]
    unexpected_hooks = [
        event_name
        for event_name, present in hooks_by_event.items()
        if present and event_name not in expected_hooks
    ]
    if missing_expected_hooks:
        issues.append(
            "one or more Compaction Sentinel hook entries are missing for hooks_profile="
            + hooks_profile
            + ": "
            + ", ".join(missing_expected_hooks)
        )
    if unexpected_hooks:
        warnings.append(
            "Compaction Sentinel has hot hooks outside hooks_profile="
            + hooks_profile
            + ": "
            + ", ".join(unexpected_hooks)
        )
    if not mcp_present:
        issues.append("compaction_sentinel MCP config is missing")
    if not hooks_feature_present:
        issues.append("Codex hooks feature is not enabled in config.toml")
    if not public_cli.exists():
        warnings.append("~/.codex/bin/cs is missing; run `~/.codex/compaction-sentinel/bin/cs doctor --fix`")
    if public_cli.exists() and not public_cli_owned:
        warnings.append("~/.codex/bin/cs exists but is not owned by Compaction Sentinel; use ~/.codex/compaction-sentinel/bin/cs")
    if public_cli_owned and not path_cli:
        warnings.append("plain `cs` is not discoverable on PATH; use ~/.codex/bin/cs or add ~/.codex/bin to PATH")
    if path_cli and not path_cli_owned:
        warnings.append(f"plain `cs` resolves to non-Sentinel command {path_cli}; use ~/.codex/bin/cs")
    missing_skills = [item["path"] for item in skill_status.values() if not item["owned"]]
    if missing_skills:
        warnings.append("Compaction Sentinel skill copy is missing or not owned: " + ", ".join(missing_skills))
    missing_global_links = [path for path, owned in global_cli_links.items() if not owned]
    if missing_global_links:
        warnings.append("recorded global Sentinel shim is missing or not owned: " + ", ".join(missing_global_links))
    if competing_injectors:
        warnings.append(
            "codex-context hook entries also inject context; run `~/.codex/bin/cs migrate codex-context --apply` or `~/.codex/bin/cs doctor --fix` to replace them"
        )
    ledger_writable = False
    ledger_error = ""
    ledger_exists = db_path(codex_home).exists()
    if ledger_exists:
        try:
            db = sqlite3.connect(f"file:{db_path(codex_home)}?mode=ro", uri=True)
            db.close()
            ledger_writable = True
        except Exception as exc:
            ledger_error = str(exc)
            warnings.append("Sentinel ledger is not readable from this environment: " + ledger_error)
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
        "path_cli": path_cli,
        "path_cli_owned": path_cli_owned,
        "skills_target": skills_target,
        "hooks_profile": hooks_profile,
        "expected_hook_events": list(hook_events_for_profile(hooks_profile)),
        "hooks_profile_matches": not missing_expected_hooks and not unexpected_hooks,
        "skill_status": skill_status,
        "skills_present": skills_present,
        "global_shim_bins": [str(path) for path in global_bins],
        "global_cli_links": global_cli_links,
        "hooks_json": str(hooks_path),
        "hooks_by_event": hooks_by_event,
        "competing_context_injectors": competing_injectors,
        "hooks_present": not missing_expected_hooks,
        "unexpected_hook_events": unexpected_hooks,
        "mcp_present": mcp_present,
        "hooks_feature_present": hooks_feature_present,
        "ledger_writable": ledger_writable,
        "ledger_exists": ledger_exists,
        "ledger_error": ledger_error,
        "performance_mode": config.get("performance_mode"),
        "auto_continue": config.get("auto_continue"),
        "retention_days": config.get("retention_days"),
        "redact": config.get("redact"),
        "backups": len(list_backups(codex_home)),
    }


def doctor_fix(*, codex_home: Path, global_bin: Path | None = None) -> dict[str, Any]:
    actions: list[str] = []
    root = install_root(codex_home)
    if not root.exists():
        raise RuntimeError("runtime is missing; run `compaction-sentinel install` from a checkout or pipx/uvx install first")
    config = load_runtime_config(codex_home)
    hooks_profile = normalize_hooks_profile(config.get("hooks_profile") or "balanced")
    replaced = replace_codex_context_hooks(codex_home)
    if replaced["changed"]:
        actions.append(
            "replaced codex-context hooks"
            + (f" ({', '.join(replaced['added'])})" if replaced.get("added") else "")
        )
    merge_hooks(codex_home, hooks_profile=hooks_profile)
    actions.append(f"merged hooks.json for hooks_profile={hooks_profile}")
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
    if "skills_target" not in config:
        config["skills_target"] = "codex"
    if "hooks_profile" not in config:
        config["hooks_profile"] = hooks_profile
    if "performance_mode" not in config:
        config["performance_mode"] = hooks_profile
    repaired_skills = repair_skill_install(codex_home, config.get("skills_target"))
    actions.append(
        "checked skill copy"
        + (f" ({', '.join(repaired_skills)})" if repaired_skills else "")
    )
    if global_bin is not None:
        remember_global_bin(config, normalize_bin_dir(global_bin))
    global_bins = configured_global_bins(config)
    for bin_dir in global_bins:
        install_global_links(bin_dir, launcher)
        actions.append(f"checked global CLI links in {bin_dir}")
    if global_bin is not None or global_bins or repaired_skills or "skills_target" in config:
        write_runtime_config(config, codex_home)
    result = doctor(codex_home=codex_home)
    result["fix_actions"] = actions
    return result


def doctor_explanations(result: dict[str, Any]) -> list[str]:
    explanations: list[str] = []
    for issue in result.get("issues", []):
        text = str(issue)
        if "hooks.json" in text:
            explanations.append("Hooks load the automatic resume packet; rerun install or `~/.codex/bin/cs doctor --fix`.")
        elif "hooks_profile" in text:
            explanations.append("The installed hook set does not match the selected hooks profile; `~/.codex/bin/cs doctor --fix` repairs it.")
        elif "MCP" in text or "mcp" in text:
            explanations.append("MCP tools let Codex explicitly save/search checkpoints; `~/.codex/bin/cs doctor --fix` rewrites the config block.")
        elif "runtime" in text:
            explanations.append("The runtime contains the hook scripts and local ledger code; reinstall from the package.")
        elif "hooks feature" in text:
            explanations.append("Codex must have `[features] hooks = true` for user-level hooks to run.")
        elif "skill copy" in text:
            explanations.append("The skill provides on-demand continuity instructions; `~/.codex/bin/cs doctor --fix` restores missing Sentinel-owned copies.")
        else:
            explanations.append(text)
    if not result.get("issues"):
        explanations.append("No blocking install issues detected.")
    for warning in result.get("warnings", []):
        text = str(warning)
        if "plain `cs` is not discoverable" in text:
            explanations.append(
                "Plain `cs` is optional. Use `~/.codex/bin/cs`, add `~/.codex/bin` to PATH, "
                "or opt in to global shims with `~/.codex/bin/cs doctor --fix --global-bin /opt/homebrew/bin`."
            )
        elif "plain `cs` resolves to non-Sentinel" in text:
            explanations.append("Another command named `cs` is earlier on PATH. Use `~/.codex/bin/cs` to avoid ambiguity.")
        elif "~/.codex/bin/cs is missing" in text:
            explanations.append("The guaranteed user-level CLI shim is missing; `doctor --fix` recreates it.")
        elif "recorded global Sentinel shim" in text:
            explanations.append("A previously opted-in global shim drifted; rerun `doctor --fix` to repair it or `uninstall` to remove it.")
        elif "skill copy" in text:
            explanations.append("The skill copy is missing or not Sentinel-owned. `doctor --fix` repairs missing copies without deleting unrelated user files.")
        elif "hot hooks outside hooks_profile" in text:
            explanations.append("Your hooks profile is light but older hot hooks are still installed. Run `~/.codex/bin/cs doctor --fix` to remove Sentinel-owned hot hooks.")
        elif "ledger is not writable" in text:
            explanations.append("The local SQLite ledger could not be opened for writes from this environment. This is often a sandbox/full-disk-access issue, not database corruption.")
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
