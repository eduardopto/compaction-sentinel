"""Core storage, hook handling, and resume-packet logic for Compaction Sentinel."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


VERSION = "0.4.6"
APP_NAME = "Compaction Sentinel"
DEFAULT_MAX_PACKET_CHARS = 5000
DEFAULT_LOOP_THRESHOLD = 3
DEFAULT_RECENT_EVENT_LIMIT = 18
DEFAULT_MAX_EVENTS_PER_PROJECT = 1200
DEFAULT_RETENTION_DAYS = 30
DEFAULT_RETENTION_CHECK_INTERVAL_SECONDS = 3600
DEFAULT_PRUNE_CHECK_INTERVAL_EVENTS = 50
DEFAULT_MAX_TOOL_INPUT_CHARS = 1000
DEFAULT_MAX_TOOL_RESPONSE_CHARS = 1200
DEFAULT_MAX_READ_ONLY_RESPONSE_CHARS = 300
PERFORMANCE_MODES = {"full", "balanced", "light"}
SENSITIVE_VALUE = "[redacted]"
REPEAT_WARNING_CATEGORIES = {
    "tool_call",
    "tool_failure",
    "stale_restart_signal",
    "oscillation_or_revert_risk",
    "completion_claim",
    "investigation",
    "permission_request",
    "prompt",
}

CHECKPOINT_TEXT_FIELDS = (
    "acceptance_criteria",
    "current_step",
    "next_action",
    "blockers",
    "evidence",
    "files_touched",
    "commands_run",
    "tests_passed",
    "tests_failed",
    "decisions_made",
    "assumptions",
    "do_not_repeat",
)

CHECKPOINT_EXTRA_COLUMNS = {
    "acceptance_criteria": "TEXT",
    "files_touched": "TEXT",
    "commands_run": "TEXT",
    "tests_passed": "TEXT",
    "tests_failed": "TEXT",
    "decisions_made": "TEXT",
    "assumptions": "TEXT",
    "do_not_repeat": "TEXT",
    "last_verified_at": "TEXT",
    "confidence": "TEXT",
}

EVENT_EXTRA_COLUMNS = {
    "tool_use_id": "TEXT",
    "event_key": "TEXT",
    "category": "TEXT",
}


@dataclass(frozen=True)
class Project:
    root: Path
    name: str


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def install_root(codex_home: Path | None = None) -> Path:
    return (codex_home or default_codex_home()) / "compaction-sentinel"


def db_path(codex_home: Path | None = None) -> Path:
    return install_root(codex_home) / "sentinel.sqlite"


def config_path(codex_home: Path | None = None) -> Path:
    return install_root(codex_home) / "config.json"


def log_path(codex_home: Path | None = None) -> Path:
    return install_root(codex_home) / "sentinel.log"


def default_runtime_config() -> dict[str, Any]:
    return {
        "version": 5,
        "max_packet_chars": DEFAULT_MAX_PACKET_CHARS,
        "loop_threshold": DEFAULT_LOOP_THRESHOLD,
        "performance_mode": "balanced",
        "hooks_profile": "balanced",
        "auto_continue": "off",
        "stop_continue_max_per_turn": 1,
        "stop_continue_max_per_checkpoint_per_turn": 1,
        "stop_continue_cooldown_seconds": 0,
        "max_events_per_project": DEFAULT_MAX_EVENTS_PER_PROJECT,
        "retention_days": DEFAULT_RETENTION_DAYS,
        "retention_check_interval_seconds": DEFAULT_RETENTION_CHECK_INTERVAL_SECONDS,
        "prune_check_interval_events": DEFAULT_PRUNE_CHECK_INTERVAL_EVENTS,
        "max_tool_input_chars": DEFAULT_MAX_TOOL_INPUT_CHARS,
        "max_tool_response_chars": DEFAULT_MAX_TOOL_RESPONSE_CHARS,
        "max_read_only_response_chars": DEFAULT_MAX_READ_ONLY_RESPONSE_CHARS,
        "redact": True,
        "skills_target": "codex",
        "global_shim_bins": [],
    }


def load_runtime_config(codex_home: Path | None = None) -> dict[str, Any]:
    path = config_path(codex_home)
    default = default_runtime_config()
    if not path.exists():
        return default
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    if isinstance(loaded, dict):
        merged = {**default, **loaded}
        try:
            if int(merged.get("version") or 0) < int(default["version"]):
                merged["version"] = default["version"]
        except Exception:
            merged["version"] = default["version"]
        return merged
    return default


def config_int(
    config: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None and value < minimum:
        return minimum
    return value


def performance_mode(config: dict[str, Any]) -> str:
    mode = str(config.get("performance_mode") or "balanced").strip().lower()
    return mode if mode in PERFORMANCE_MODES else "balanced"


def write_runtime_config(config: dict[str, Any], codex_home: Path | None = None) -> None:
    root = install_root(codex_home)
    root.mkdir(parents=True, exist_ok=True)
    path = config_path(codex_home)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def log(message: str, codex_home: Path | None = None) -> None:
    try:
        root = install_root(codex_home)
        root.mkdir(parents=True, exist_ok=True)
        with log_path(codex_home).open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_now()} {message}\n")
    except Exception:
        pass


def connect(codex_home: Path | None = None) -> sqlite3.Connection:
    path = db_path(codex_home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            "Sentinel ledger directory is not writable from this environment. "
            f"Path: {path.parent}. If this came from a sandboxed shell, use MCP/hooks, "
            "grant filesystem access, or run ~/.codex/bin/cs outside the sandbox."
        ) from exc
    db: sqlite3.Connection | None = None
    try:
        db = sqlite3.connect(path, timeout=5.0)
        db.row_factory = sqlite3.Row
        init_db(db)
        return db
    except sqlite3.OperationalError as exc:
        if db is not None:
            db.close()
        message = str(exc)
        if "unable to open database file" in message or "database is locked" in message:
            raise RuntimeError(
                "Sentinel ledger is not writable from this environment. "
                f"Path: {path}. If this came from a sandboxed shell, use MCP/hooks, "
                "grant filesystem access, or run ~/.codex/bin/cs outside the sandbox."
            ) from exc
        raise


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY,
          project_root TEXT NOT NULL,
          project_name TEXT NOT NULL,
          session_id TEXT,
          turn_id TEXT,
          event_name TEXT NOT NULL,
          kind TEXT NOT NULL,
          category TEXT,
          summary TEXT NOT NULL,
          details_json TEXT NOT NULL,
          fingerprint TEXT,
          tool_use_id TEXT,
          event_key TEXT,
          outcome TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_project_id
          ON events(project_root, id DESC);
        CREATE INDEX IF NOT EXISTS idx_events_fingerprint
          ON events(project_root, fingerprint, id DESC);
        CREATE INDEX IF NOT EXISTS idx_events_created_at
          ON events(created_at);
        CREATE TABLE IF NOT EXISTS checkpoints (
          id INTEGER PRIMARY KEY,
          project_root TEXT NOT NULL,
          project_name TEXT NOT NULL,
          session_id TEXT,
          turn_id TEXT,
          status TEXT NOT NULL DEFAULT 'active',
          objective TEXT NOT NULL,
          acceptance_criteria TEXT,
          current_step TEXT,
          next_action TEXT,
          blockers TEXT,
          evidence TEXT,
          files_touched TEXT,
          commands_run TEXT,
          tests_passed TEXT,
          tests_failed TEXT,
          decisions_made TEXT,
          assumptions TEXT,
          do_not_repeat TEXT,
          last_verified_at TEXT,
          confidence TEXT,
          source TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_checkpoints_project_id
          ON checkpoints(project_root, id DESC);
        CREATE INDEX IF NOT EXISTS idx_checkpoints_created_at
          ON checkpoints(created_at);

        CREATE TABLE IF NOT EXISTS notes (
          id INTEGER PRIMARY KEY,
          project_root TEXT NOT NULL,
          project_name TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          content TEXT NOT NULL,
          surface_condition TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notes_project_status
          ON notes(project_root, status, id DESC);
        CREATE INDEX IF NOT EXISTS idx_notes_created_at
          ON notes(created_at);

        CREATE TABLE IF NOT EXISTS state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    migrate_columns(db, "events", EVENT_EXTRA_COLUMNS)
    checkpoint_columns = {"completed_at": "TEXT", **CHECKPOINT_EXTRA_COLUMNS}
    migrate_columns(db, "checkpoints", checkpoint_columns)
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_project_event_key
          ON events(project_root, event_key)
          WHERE event_key IS NOT NULL
        """
    )
    migrate_event_categories(db)
    db.execute("PRAGMA busy_timeout=2000;")
    db.commit()


def migrate_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, sql_type in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def migrate_event_categories(db: sqlite3.Connection) -> None:
    row = db.execute("PRAGMA user_version").fetchone()
    if row and int(row[0]) >= 4:
        return
    rows = db.execute(
        "SELECT id, event_name, kind, summary, outcome, category FROM events ORDER BY id"
    ).fetchall()
    for event in rows:
        category = event_category(
            str(event["event_name"] or ""),
            str(event["kind"] or ""),
            str(event["summary"] or ""),
            event["outcome"],
        )
        if category != str(event["category"] or ""):
            db.execute("UPDATE events SET category = ? WHERE id = ?", (category, event["id"]))
    db.execute("PRAGMA user_version = 4")


def read_json_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def safe_print_json(value: dict[str, Any]) -> None:
    sys.stdout.write(dump_json(value) + "\n")


PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

SECRET_PATTERNS = [
    re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?im)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTHORIZATION|ACCESS[_-]?KEY|PRIVATE[_-]?KEY)[A-Z0-9_]*)"
        r"\s*[:=]\s*['\"]?[^'\"\s,;]{6,}"
    ),
]

HIGH_ENTROPY_RE = re.compile(
    r"(?<![A-Za-z0-9_./=-])(?=[A-Za-z0-9+/=_-]{32,})(?=.*[A-Z])(?=.*[a-z])(?=.*\d)"
    r"[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9_./=-])"
)


def redact_text(text: str, *, enabled: bool = True) -> str:
    if not text:
        return ""
    if not enabled:
        return text
    redacted = PRIVATE_KEY_RE.sub(SENSITIVE_VALUE, text)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: _redact_match(m.group(0)), redacted)
    redacted = HIGH_ENTROPY_RE.sub(SENSITIVE_VALUE, redacted)
    return redacted


def _redact_match(value: str) -> str:
    if value.lower().startswith("bearer "):
        return "Bearer " + SENSITIVE_VALUE
    if ":" in value or "=" in value:
        key = re.split(r"[:=]", value, maxsplit=1)[0].strip()
        return f"{key}={SENSITIVE_VALUE}"
    if value.startswith("sk-"):
        return "sk-" + SENSITIVE_VALUE
    if value.startswith("ghp_"):
        return "ghp_" + SENSITIVE_VALUE
    if value.startswith("github_pat_"):
        return "github_pat_" + SENSITIVE_VALUE
    if value.startswith("xox"):
        return "xox-" + SENSITIVE_VALUE
    return SENSITIVE_VALUE


def normalize_text(value: Any, limit: int = 3000, *, redact: bool = True) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(value)
    text = redact_text(text, enabled=redact)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 20].rstrip() + " ...[truncated]"
    return text


def stringify_for_storage(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def compact_by_lines(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 24:
        return text[:limit]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 2:
        return text[: limit - 20].rstrip() + " ...[truncated]"
    marker = "...[middle truncated]..."
    first: list[str] = []
    last: list[str] = []
    used = len(marker) + 2
    for line in lines:
        cost = len(line) + 1
        if used + cost > limit // 2:
            break
        first.append(line)
        used += cost
    for line in reversed(lines):
        cost = len(line) + 1
        if used + cost > limit:
            break
        last.insert(0, line)
        used += cost
    compacted = "\n".join([*first, marker, *last]).strip()
    if len(compacted) > limit:
        return compacted[: limit - 20].rstrip() + " ...[truncated]"
    return compacted


def failure_context_text(text: str, limit: int) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return str(text or "")
    interesting_indexes = [
        index
        for index, line in enumerate(lines)
        if re.search(r"(?i)\b(failed|failure|traceback|exception|fatal|assert|error|exit[_ -]?code)\b", line)
    ]
    if not interesting_indexes:
        return compact_by_lines(text, limit)
    selected: list[str] = []
    seen: set[int] = set()
    for index in interesting_indexes[:3]:
        for candidate in range(max(0, index - 2), min(len(lines), index + 4)):
            if candidate not in seen:
                selected.append(lines[candidate])
                seen.add(candidate)
    return compact_by_lines("\n".join(selected), limit)


def compact_tool_response_text(
    command: str,
    response: Any,
    *,
    outcome: str | None = None,
    config: dict[str, Any] | None = None,
    redact: bool = True,
) -> str:
    config = config or {}
    raw = redact_text(stringify_for_storage(response), enabled=redact).strip()
    if not raw:
        return ""
    command_text = command_for_classification(command)
    max_response = config_int(config, "max_tool_response_chars", DEFAULT_MAX_TOOL_RESPONSE_CHARS, minimum=120)
    read_only_limit = config_int(
        config,
        "max_read_only_response_chars",
        DEFAULT_MAX_READ_ONLY_RESPONSE_CHARS,
        minimum=80,
    )
    summary = f"Bash: {command} -> {raw}"
    failed = is_failure_summary(summary, outcome)
    if is_read_only_command(command_text) and not failed:
        return normalize_text(compact_by_lines(raw, read_only_limit), read_only_limit, redact=False)
    if is_test_or_build_command(command_text) and not failed:
        return normalize_text(compact_by_lines(raw, max_response), max_response, redact=False)
    if failed:
        return normalize_text(failure_context_text(raw, max_response), max_response, redact=False)
    return normalize_text(raw, max_response, redact=False)


def sanitize_json(value: Any, *, text_limit: int = 3000, redact: bool = True) -> Any:
    if isinstance(value, str):
        return normalize_text(value, text_limit, redact=redact)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = normalize_text(str(key), 200, redact=redact)
            if redact and re.search(
                r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|private[_-]?key)",
                safe_key,
            ):
                out[safe_key] = SENSITIVE_VALUE
            else:
                out[safe_key] = sanitize_json(item, text_limit=text_limit, redact=redact)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item, text_limit=text_limit, redact=redact) for item in value[:50]]
    return normalize_text(value, text_limit, redact=redact)


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def fingerprint(kind: str, summary: str) -> str:
    normalized = normalize_for_fingerprint(summary)
    return hash_text(f"{kind}:{normalized}")[:24]


def normalize_for_fingerprint(text: str) -> str:
    normalized = redact_text(text).lower()
    normalized = re.sub(r"/Users/[^ ]+", "/PATH", normalized)
    normalized = re.sub(r"/private/var/folders/[^ ]+", "/TMP", normalized)
    normalized = re.sub(r"\b[0-9a-f]{7,64}\b", "HASH", normalized)
    normalized = re.sub(r"\b\d{4}-\d{2}-\d{2}[t ][0-9:.+-z]+\b", "TIME", normalized)
    normalized = re.sub(r"\b\d+\b", "N", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def payload_cwd(payload: dict[str, Any]) -> Path:
    candidates = [
        payload.get("cwd"),
        payload.get("workspace"),
        payload.get("workspace_path"),
        payload.get("project_path"),
    ]
    workspace = payload.get("workspace")
    if isinstance(workspace, dict):
        candidates.extend([workspace.get("cwd"), workspace.get("path")])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return Path(candidate).expanduser()
    return Path.cwd()


def find_project_root(start: Path) -> Path:
    current = start if start.is_dir() else start.parent
    current = current.expanduser().resolve()
    markers = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", ".codex", ".agents")
    for candidate in [current, *current.parents]:
        if any((candidate / marker).exists() for marker in markers):
            return candidate
    return current


def project_from_payload(payload: dict[str, Any]) -> Project:
    root = find_project_root(payload_cwd(payload))
    return Project(root=root, name=root.name or "workspace")


def get_nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def extract_prompt(payload: dict[str, Any], *, redact: bool = True) -> str:
    for key in ("prompt", "user_prompt", "message", "input", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value, 6000, redact=redact)
    return ""


def extract_tool_name(payload: dict[str, Any]) -> str:
    for key in ("tool_name", "name", "tool"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    tool = payload.get("tool")
    if isinstance(tool, dict):
        value = tool.get("name")
        if isinstance(value, str) and value.strip():
            return value
    return "tool"


def extract_tool_use_id(payload: dict[str, Any]) -> str:
    for key in ("tool_use_id", "tool_call_id", "call_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value, 200)
    return ""


def extract_tool_input(
    payload: dict[str, Any],
    *,
    redact: bool = True,
    limit: int = DEFAULT_MAX_TOOL_INPUT_CHARS,
) -> str:
    value = payload.get("tool_input")
    if value is None:
        value = payload.get("input")
    if isinstance(value, dict):
        command = value.get("command") or value.get("cmd")
        if isinstance(command, str) and command.strip():
            return normalize_text(command, limit, redact=redact)
    return normalize_text(value, limit, redact=redact)


def extract_permission_reason(payload: dict[str, Any], *, redact: bool = True) -> str:
    for path in (("tool_input", "description"), ("input", "description")):
        value = get_nested(payload, *path)
        if isinstance(value, str) and value.strip():
            return normalize_text(value, 1200, redact=redact)
    for key in ("description", "reason", "justification"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value, 1200, redact=redact)
    return ""


def extract_tool_response(
    payload: dict[str, Any],
    *,
    redact: bool = True,
    command: str = "",
    config: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    value = payload.get("tool_response")
    if value is None:
        value = payload.get("response") or payload.get("output") or payload.get("result")
    outcome = None
    if isinstance(value, dict):
        for key in ("exit_code", "status", "returncode", "code"):
            if key in value:
                outcome = str(value.get(key))
                break
        combined = {
            key: value.get(key)
            for key in ("stdout", "stderr", "output", "error", "status", "exit_code", "result")
            if key in value
        }
        return compact_tool_response_text(
            command,
            combined or value,
            outcome=outcome,
            config=config,
            redact=redact,
        ), outcome
    return compact_tool_response_text(command, value, outcome=outcome, config=config, redact=redact), outcome


def extract_last_assistant(payload: dict[str, Any], *, redact: bool = True) -> str:
    for key in ("last_assistant_message", "assistant_message", "message", "output"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value, 4000, redact=redact)
    return ""


def event_identity(payload: dict[str, Any], event_name: str) -> tuple[str, str]:
    tool_use_id = extract_tool_use_id(payload)
    if not tool_use_id:
        return "", ""
    session_id = str(payload.get("session_id") or "")
    turn_id = str(payload.get("turn_id") or "")
    return tool_use_id, hash_text(f"{session_id}:{turn_id}:{event_name}:{tool_use_id}")[:40]


def event_category(event_name: str, kind: str, summary: str, outcome: str | None = None) -> str:
    lowered = summary.lower()
    if event_name == "PermissionRequest":
        return "permission_request"
    if event_name == "UserPromptSubmit":
        if re.search(r"\b(start over|from scratch|restart)\b", lowered):
            return "stale_restart_signal"
        return "prompt"
    if event_name == "PreToolUse":
        if re.search(r"\b(cat|sed|rg|grep|find|ls|git show|git diff|tail|head)\b", lowered):
            return "investigation"
        if re.search(r"\b(git checkout|git restore|revert|reset --hard)\b", lowered):
            return "oscillation_or_revert_risk"
        return "tool_call"
    if event_name == "PostToolUse":
        if is_failure_summary(summary, outcome):
            return "tool_failure"
        if is_success_summary(summary, outcome):
            return "tool_success"
        return "tool_result"
    if event_name == "Stop":
        if looks_complete(summary):
            return "completion_claim"
        return "stop"
    return kind


COMPLETION_NEGATIVE_RE = re.compile(
    r"(?i)\b("
    r"not\s+done|not\s+complete|not\s+completed|still\s+failing|still\s+fails|"
    r"remaining|need\s+to|needs\s+to|blocked|failed|failing|not\s+verified|"
    r"unverified|could\s+not|can't|cannot|pending|todo|wip"
    r")\b"
)

COMPLETION_POSITIVE_RE = re.compile(
    r"(?i)\b("
    r"goal\s+complete|marked\s+the\s+goal\s+complete|completed\s+and\s+verified|"
    r"verified\s+complete|all\s+tests\s+passed|ci\s+is\s+green|ci\s+green|"
    r"shipped|implemented\s+and\s+verified|done\s+and\s+verified|is\s+complete"
    r")\b"
)


SUCCESS_OUTCOMES = {"0", "success", "succeeded", "ok", "none", "true", "completed"}
FAILURE_OUTCOME_RE = re.compile(
    r"(?i)(?:^|[\s,{])(?:exit[_ -]?code|return[_ -]?code|returncode|status|code)[\"']?\s*[:=]\s*[1-9]\d*"
)
READ_ONLY_COMMAND_RE = re.compile(
    r"(?is)^\s*(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:(?:cat|sed|awk|grep|rg|find|ls|head|tail|nl|wc|file|stat|du|tree|less|more)\b|"
    r"git\s+(?:show|diff|status|log|rev-parse|ls-files|branch|tag|describe|remote|config)\b|"
    r"python3?\s+-m\s+json\.tool\b|"
    r"jq\b|"
    r"sqlite3\b.*\bselect\b)"
)
TEST_OR_BUILD_COMMAND_RE = re.compile(
    r"(?is)\b("
    r"pytest|unittest|tox|nox|coverage|make(?:\s+\S*)?\s+(?:test|tests|check|compile|build|replay)|"
    r"npm\s+(?:test|run\s+(?:test|build|lint|typecheck))|"
    r"pnpm\s+(?:test|run\s+(?:test|build|lint|typecheck))|"
    r"yarn\s+(?:test|run\s+(?:test|build|lint|typecheck))|"
    r"vitest|jest|mocha|playwright|xcodebuild|swift\s+test|cargo\s+test|go\s+test|"
    r"mvn\s+test|gradle\s+test|ruff|mypy|tsc|eslint"
    r")\b"
)
READ_ONLY_FAILURE_RE = re.compile(
    r"(?i)^\s*("
    r"(?:zsh|bash|sh):\d*:|"
    r"(?:cat|sed|awk|grep|rg|find|ls|head|tail|nl|wc|file|stat|du|tree|python(?:3)?|jq|sqlite3):\s+"
    r"(?:[^:]{1,240}:\s+)?"
    r"(?:can't open|cannot open|no such file|permission denied|operation not permitted|not found|fatal|error)|"
    r"can't open file|no such file or directory|permission denied|operation not permitted|not a directory|is a directory|"
    r"fatal:\s+not a git repository"
    r")"
)
READ_ONLY_VALIDATION_FAILURE_RE = re.compile(
    r"(?i)^\s*(parse error:|regex parse error:|json\.decoder\.jsondecodeerror|"
    r"expecting value:\s+line\s+\d+\s+column\s+\d+)"
)
EXECUTION_FAILURE_RE = re.compile(
    r"(?i)(?:^|\s)("
    r"traceback \(most recent call last\)|assertionerror|moduleNotFoundError|importError|syntaxError|"
    r"typeError|valueError|uncaught exception|segmentation fault|command not found|"
    r"build failed|compilation failed|fatal error|error:"
    r")(?:\s|$|:)"
)
TEST_FAILURE_RE = re.compile(
    r"(?i)(?:^|\s)("
    r"FAILED\b|ERROR\b|tests?\s+failed|failed\s+tests?|[1-9]\d*\s+failed|"
    r"failures?[=:]\s*[1-9]\d*|errors?[=:]\s*[1-9]\d*|"
    r"assertionerror|traceback \(most recent call last\)|build failed|compilation failed"
    r")(?:\s|$|:)"
)
ZERO_FAILURE_COUNT_RE = re.compile(
    r"(?i)\b(?:0|zero|no)\s+(?:tests?\s+)?(?:failed|failures?|errors?|failed\s+tests?)\b|"
    r"\b(?:no|zero)\s+tests?\s+failed\b"
)


def tool_summary_parts(summary: str) -> tuple[str, str, str]:
    left, sep, response = summary.partition(" -> ")
    if ": " in left:
        tool_name, tool_input = left.split(": ", 1)
    else:
        tool_name, tool_input = "", left
    return tool_name.strip(), tool_input.strip(), response.strip() if sep else ""


def normalized_outcome(outcome: str | None) -> str:
    return str(outcome or "").strip().lower()


def shell_quote_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def strip_env_prefix(parts: list[str]) -> list[str]:
    remaining = list(parts)
    while remaining:
        token = remaining[0]
        if token == "env":
            remaining = remaining[1:]
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", token):
            remaining = remaining[1:]
            continue
        break
    return remaining


def command_for_classification(command: str) -> str:
    """Unwrap common shell/package runners before command-type detection."""
    current = str(command or "").strip()
    for _ in range(4):
        try:
            parts = shlex.split(current)
        except ValueError:
            return current
        parts = strip_env_prefix(parts)
        if not parts:
            return current
        runner = parts[0]
        if runner in {"bash", "zsh", "sh"}:
            replacement = ""
            for index, token in enumerate(parts[1:], start=1):
                if token == "-c" or (token.startswith("-") and "c" in token):
                    if index + 1 < len(parts):
                        replacement = parts[index + 1]
                    break
            if replacement and replacement != current:
                current = replacement.strip()
                continue
            return current
        if runner in {"uv", "poetry"} and len(parts) >= 3 and parts[1] == "run":
            replacement = shell_quote_join(parts[2:]).strip()
            if replacement and replacement != current:
                current = replacement
                continue
        return current
    return current


def outcome_indicates_failure(outcome: str | None) -> bool:
    value = normalized_outcome(outcome)
    if not value or value in SUCCESS_OUTCOMES:
        return False
    if value.isdigit():
        return int(value) != 0
    if FAILURE_OUTCOME_RE.search(value):
        return True
    return bool(re.search(r"(?i)\b(fail|failed|failure|error|fatal|denied|timeout|cancelled)\b", value))


def outcome_indicates_success(outcome: str | None) -> bool:
    return normalized_outcome(outcome) in SUCCESS_OUTCOMES


def is_read_only_command(command: str) -> bool:
    return bool(READ_ONLY_COMMAND_RE.search(command_for_classification(command)))


def is_test_or_build_command(command: str) -> bool:
    return bool(TEST_OR_BUILD_COMMAND_RE.search(command_for_classification(command)))


def read_only_response_failed(command: str, response: str) -> bool:
    if READ_ONLY_FAILURE_RE.search(response):
        return True
    lowered = command_for_classification(command).lower()
    if re.search(r"^\s*(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*git\b", lowered):
        return bool(re.search(r"(?i)^\s*(fatal|error):\s+", response))
    if re.search(r"^\s*(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*jq\b", lowered):
        return bool(READ_ONLY_VALIDATION_FAILURE_RE.search(response))
    if re.search(r"^\s*(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*rg\b", lowered):
        return bool(READ_ONLY_VALIDATION_FAILURE_RE.search(response))
    if re.search(r"^\s*(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*python3?\s+-m\s+json\.tool\b", lowered):
        return bool(READ_ONLY_VALIDATION_FAILURE_RE.search(response))
    return False


def is_failure_summary(summary: str, outcome: str | None = None) -> bool:
    if outcome_indicates_failure(outcome):
        return True
    if FAILURE_OUTCOME_RE.search(summary):
        return True
    _tool_name, command, response = tool_summary_parts(summary)
    haystack = response or summary
    if is_read_only_command(command):
        return read_only_response_failed(command, haystack)
    if EXECUTION_FAILURE_RE.search(haystack):
        return True
    if is_test_or_build_command(command):
        test_haystack = ZERO_FAILURE_COUNT_RE.sub(" ", haystack)
        return bool(TEST_FAILURE_RE.search(test_haystack))
    return False


def is_success_summary(summary: str, outcome: str | None = None) -> bool:
    if outcome_indicates_success(outcome):
        return True
    _tool_name, command, response = tool_summary_parts(summary)
    if not is_test_or_build_command(command):
        return False
    return bool(re.search(r"(?i)\b(all tests passed|passed|success|succeeded|0 failed)\b", response or summary))


def looks_complete(text: str) -> bool:
    normalized = normalize_text(text, 4000).lower()
    if not normalized:
        return False
    if COMPLETION_NEGATIVE_RE.search(normalized):
        return False
    return bool(COMPLETION_POSITIVE_RE.search(normalized))


def record_event(
    db: sqlite3.Connection,
    project: Project,
    payload: dict[str, Any],
    *,
    event_name: str,
    kind: str,
    summary: str,
    details: dict[str, Any] | None = None,
    outcome: str | None = None,
    redact: bool = True,
    max_events: int = DEFAULT_MAX_EVENTS_PER_PROJECT,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    retention_check_interval_seconds: int = DEFAULT_RETENTION_CHECK_INTERVAL_SECONDS,
    prune_check_interval_events: int = DEFAULT_PRUNE_CHECK_INTERVAL_EVENTS,
) -> sqlite3.Row:
    clean_summary = normalize_text(summary, 4000, redact=redact)
    clean_details = sanitize_json(details or {}, redact=redact)
    now = utc_now()
    tool_use_id, event_key = event_identity(payload, event_name)
    fp = fingerprint(kind, clean_summary) if clean_summary else None
    category = event_category(event_name, kind, clean_summary, outcome)
    params = (
        str(project.root),
        project.name,
        str(payload.get("session_id") or ""),
        str(payload.get("turn_id") or ""),
        event_name,
        kind,
        category,
        clean_summary,
        json.dumps(clean_details, ensure_ascii=False, sort_keys=True),
        fp,
        tool_use_id or None,
        event_key or None,
        outcome,
        now,
    )
    db.execute(
        """
        INSERT OR IGNORE INTO events
          (project_root, project_name, session_id, turn_id, event_name, kind, category,
           summary, details_json, fingerprint, tool_use_id, event_key, outcome, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params,
    )
    db.commit()
    if event_key:
        row = db.execute(
            "SELECT * FROM events WHERE project_root = ? AND event_key = ? ORDER BY id DESC LIMIT 1",
            (str(project.root), event_key),
        ).fetchone()
    else:
        row = db.execute("SELECT * FROM events WHERE id = last_insert_rowid()").fetchone()
    maybe_prune_events(
        db,
        project,
        max_events=max_events,
        interval_events=prune_check_interval_events,
    )
    maybe_apply_retention(
        db,
        retention_days=retention_days,
        interval_seconds=retention_check_interval_seconds,
    )
    assert row is not None
    return row


def maintenance_last_retention_key() -> str:
    return "maintenance:last_retention_at"


def project_prune_count_key(project: Project) -> str:
    return f"{project_state_prefix(project)}maintenance:event_count_since_prune"


def maybe_apply_retention(
    db: sqlite3.Connection,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    interval_seconds: int = DEFAULT_RETENTION_CHECK_INTERVAL_SECONDS,
) -> None:
    if retention_days <= 0:
        return
    key = maintenance_last_retention_key()
    if interval_seconds > 0 and within_cooldown(get_state(db, key), interval_seconds):
        return
    apply_retention(db, retention_days=retention_days)
    set_state(db, key, utc_now())


def maybe_prune_events(
    db: sqlite3.Connection,
    project: Project,
    *,
    max_events: int = DEFAULT_MAX_EVENTS_PER_PROJECT,
    interval_events: int = DEFAULT_PRUNE_CHECK_INTERVAL_EVENTS,
) -> None:
    if max_events <= 0:
        return
    if interval_events <= 1:
        prune_events(db, project, max_events=max_events)
        set_state(db, project_prune_count_key(project), "0")
        return
    key = project_prune_count_key(project)
    count = state_count(db, key) + 1
    if count < interval_events:
        set_state(db, key, str(count))
        return
    prune_events(db, project, max_events=max_events)
    set_state(db, key, "0")


def prune_events(
    db: sqlite3.Connection,
    project: Project,
    *,
    max_events: int = DEFAULT_MAX_EVENTS_PER_PROJECT,
) -> None:
    if max_events <= 0:
        return
    row = db.execute(
        "SELECT COUNT(*) AS count FROM events WHERE project_root = ?",
        (str(project.root),),
    ).fetchone()
    if not row or int(row["count"]) <= max_events:
        return
    db.execute(
        """
        DELETE FROM events
        WHERE project_root = ?
          AND id NOT IN (
            SELECT id FROM events
            WHERE project_root = ?
            ORDER BY id DESC
            LIMIT ?
          )
        """,
        (str(project.root), str(project.root), max_events),
    )
    db.commit()


def apply_retention(db: sqlite3.Connection, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
    if retention_days <= 0:
        return
    cutoff = (
        dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        - dt.timedelta(days=retention_days)
    ).isoformat()
    db.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
    db.execute("DELETE FROM notes WHERE created_at < ? AND status != 'open'", (cutoff,))
    db.execute(
        """
        DELETE FROM checkpoints
        WHERE created_at < ?
          AND status NOT IN ('active', 'blocked')
        """,
        (cutoff,),
    )
    db.commit()


def infer_objective(prompt: str) -> str | None:
    if not prompt:
        return None
    patterns = [
        r"(?ims)^\s*set\s+goal\s*:\s*(.+)",
        r"(?ims)^\s*goal\s*:\s*(.+)",
        r"(?ims)^\s*do\s+not\s+stop\s+until\s+(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, prompt)
        if match:
            objective = match.group(1).strip()
            objective = re.split(r"\n\s*\n", objective, maxsplit=1)[0].strip()
            objective = re.sub(r"\s+", " ", objective)
            if len(objective) > 1400:
                objective = objective[:1380].rstrip() + " ..."
            if objective:
                return objective
    return None


def save_checkpoint(
    db: sqlite3.Connection,
    project: Project,
    *,
    objective: str,
    status: str = "active",
    acceptance_criteria: str = "",
    current_step: str = "",
    next_action: str = "",
    blockers: str = "",
    evidence: str = "",
    files_touched: str = "",
    commands_run: str = "",
    tests_passed: str = "",
    tests_failed: str = "",
    decisions_made: str = "",
    assumptions: str = "",
    do_not_repeat: str = "",
    last_verified_at: str = "",
    confidence: str = "",
    source: str = "manual",
    session_id: str = "",
    turn_id: str = "",
    redact: bool = True,
) -> int:
    clean_objective = normalize_text(objective, 1400, redact=redact)
    if not clean_objective:
        raise ValueError("checkpoint objective is required")
    now = utc_now()
    clean_status = normalize_text(status, 80, redact=redact) or "active"
    if clean_status not in {"active", "blocked", "complete", "superseded"}:
        clean_status = "active"
    close_status = "complete" if clean_status == "complete" else "superseded"
    db.execute(
        """
        UPDATE checkpoints
        SET status = ?, completed_at = COALESCE(completed_at, ?), updated_at = ?
        WHERE project_root = ? AND status IN ('active', 'blocked')
        """,
        (close_status, now, now, str(project.root)),
    )
    normalized_fields = {
        "acceptance_criteria": normalize_text(acceptance_criteria, 2400, redact=redact),
        "current_step": normalize_text(current_step, 1400, redact=redact),
        "next_action": normalize_text(next_action, 1400, redact=redact),
        "blockers": normalize_text(blockers, 1400, redact=redact),
        "evidence": normalize_text(evidence, 2400, redact=redact),
        "files_touched": normalize_text(files_touched, 1600, redact=redact),
        "commands_run": normalize_text(commands_run, 2400, redact=redact),
        "tests_passed": normalize_text(tests_passed, 1600, redact=redact),
        "tests_failed": normalize_text(tests_failed, 1600, redact=redact),
        "decisions_made": normalize_text(decisions_made, 1600, redact=redact),
        "assumptions": normalize_text(assumptions, 1600, redact=redact),
        "do_not_repeat": normalize_text(do_not_repeat, 1800, redact=redact),
        "last_verified_at": normalize_text(last_verified_at, 200, redact=redact),
        "confidence": normalize_text(confidence, 80, redact=redact),
    }
    if not normalized_fields["last_verified_at"] and (
        normalized_fields["evidence"] or normalized_fields["tests_passed"]
    ):
        normalized_fields["last_verified_at"] = now
    db.execute(
        """
        INSERT INTO checkpoints
          (project_root, project_name, session_id, turn_id, status, objective,
           acceptance_criteria, current_step, next_action, blockers, evidence,
           files_touched, commands_run, tests_passed, tests_failed, decisions_made,
           assumptions, do_not_repeat, last_verified_at, confidence, source,
           created_at, updated_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(project.root),
            project.name,
            session_id,
            turn_id,
            clean_status,
            clean_objective,
            normalized_fields["acceptance_criteria"],
            normalized_fields["current_step"],
            normalized_fields["next_action"],
            normalized_fields["blockers"],
            normalized_fields["evidence"],
            normalized_fields["files_touched"],
            normalized_fields["commands_run"],
            normalized_fields["tests_passed"],
            normalized_fields["tests_failed"],
            normalized_fields["decisions_made"],
            normalized_fields["assumptions"],
            normalized_fields["do_not_repeat"],
            normalized_fields["last_verified_at"],
            normalized_fields["confidence"],
            source,
            now,
            now,
            now if clean_status == "complete" else None,
        ),
    )
    db.commit()
    row = db.execute("SELECT last_insert_rowid() AS id").fetchone()
    return int(row["id"])


def latest_checkpoint(db: sqlite3.Connection, project: Project) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT * FROM checkpoints
        WHERE project_root = ?
        ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END, id DESC
        LIMIT 1
        """,
        (str(project.root),),
    ).fetchone()


def active_checkpoint(db: sqlite3.Connection, project: Project) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT * FROM checkpoints
        WHERE project_root = ? AND status IN ('active', 'blocked')
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(project.root),),
    ).fetchone()


def checkpoint_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def update_checkpoint_from_prompt(
    db: sqlite3.Connection,
    project: Project,
    payload: dict[str, Any],
    prompt: str,
    *,
    redact: bool = True,
) -> int | None:
    objective = infer_objective(prompt)
    if not objective:
        return None
    current = active_checkpoint(db, project)
    if current and normalize_text(current["objective"], 900, redact=redact) == normalize_text(
        objective, 900, redact=redact
    ):
        return int(current["id"])
    return save_checkpoint(
        db,
        project,
        objective=objective,
        status="active",
        current_step="User set or refreshed the active goal.",
        next_action="Continue from the active task state; do not restart from stale context.",
        acceptance_criteria="Preserve the user's latest natural-language finish line.",
        source="UserPromptSubmit",
        session_id=str(payload.get("session_id") or ""),
        turn_id=str(payload.get("turn_id") or ""),
        redact=redact,
    )


def save_note(
    db: sqlite3.Connection,
    project: Project,
    content: str,
    *,
    surface_condition: str = "",
    status: str = "open",
    redact: bool = True,
) -> int:
    clean_content = normalize_text(content, 3000, redact=redact)
    if not clean_content:
        raise ValueError("note content is required")
    now = utc_now()
    db.execute(
        """
        INSERT INTO notes
          (project_root, project_name, status, content, surface_condition, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(project.root),
            project.name,
            normalize_text(status, 80, redact=redact) or "open",
            clean_content,
            normalize_text(surface_condition, 1000, redact=redact),
            now,
            now,
        ),
    )
    db.commit()
    row = db.execute("SELECT last_insert_rowid() AS id").fetchone()
    return int(row["id"])


def append_checkpoint_field(
    db: sqlite3.Connection,
    project: Project,
    *,
    field: str,
    value: str,
    objective: str | None = None,
    redact: bool = True,
) -> int:
    if field not in CHECKPOINT_TEXT_FIELDS:
        raise ValueError(f"unsupported checkpoint field: {field}")
    checkpoint = active_checkpoint(db, project)
    if checkpoint is None:
        if not objective:
            raise ValueError("no active checkpoint; pass objective or create a checkpoint first")
        checkpoint_id = save_checkpoint(db, project, objective=objective, redact=redact)
        checkpoint = active_checkpoint(db, project)
        if checkpoint is None:
            return checkpoint_id
    clean_value = normalize_text(value, 1800, redact=redact)
    if not clean_value:
        raise ValueError("value is required")
    existing = str(checkpoint[field] or "")
    combined = append_line(existing, clean_value)
    now = utc_now()
    last_verified = now if field in {"evidence", "tests_passed", "tests_failed", "commands_run"} else checkpoint["last_verified_at"]
    db.execute(
        f"UPDATE checkpoints SET {field} = ?, last_verified_at = ?, updated_at = ? WHERE id = ?",
        (combined, last_verified, now, int(checkpoint["id"])),
    )
    db.commit()
    return int(checkpoint["id"])


def append_line(existing: str, new_value: str) -> str:
    items = [line.strip(" -") for line in existing.splitlines() if line.strip()]
    if new_value not in items:
        items.append(new_value)
    return "\n".join(f"- {item}" for item in items)


def recent_events(
    db: sqlite3.Connection, project: Project, limit: int = DEFAULT_RECENT_EVENT_LIMIT
) -> list[sqlite3.Row]:
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(project.root), limit),
    ).fetchall()
    return list(reversed(rows))


def recent_notes(db: sqlite3.Connection, project: Project, limit: int = 5) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT * FROM notes
        WHERE project_root = ? AND status = 'open'
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(project.root), limit),
    ).fetchall()


def loop_warnings(
    db: sqlite3.Connection,
    project: Project,
    *,
    fingerprint_value: str | None = None,
    threshold: int = DEFAULT_LOOP_THRESHOLD,
    include_failure_loop: bool = True,
    include_investigation_loop: bool = True,
    include_tool_output_blindness: bool = True,
) -> list[str]:
    warnings = repeat_warnings(
        db,
        project,
        fingerprint_value=fingerprint_value,
        threshold=threshold,
    )
    if include_failure_loop:
        warnings.extend(failure_loop_warnings(db, project, threshold=threshold))
    if include_investigation_loop:
        warnings.extend(investigation_loop_warnings(db, project, threshold=max(threshold + 1, 4)))
    if include_tool_output_blindness:
        warnings.extend(tool_output_blindness_warnings(db, project))
    return dedupe_keep_order(warnings)[:8]


def repeat_warnings(
    db: sqlite3.Connection,
    project: Project,
    *,
    fingerprint_value: str | None = None,
    threshold: int = DEFAULT_LOOP_THRESHOLD,
) -> list[str]:
    warnings: list[str] = []
    clauses = ["project_root = ?", "fingerprint IS NOT NULL"]
    params: list[Any] = [str(project.root)]
    if fingerprint_value:
        clauses.append("fingerprint = ?")
        params.append(fingerprint_value)
    rows = db.execute(
        f"""
        SELECT fingerprint, kind, COUNT(*) AS count, MAX(id) AS last_id
        FROM (
          SELECT * FROM events
          WHERE {" AND ".join(clauses)}
          ORDER BY id DESC
          LIMIT 80
        )
        GROUP BY project_root, fingerprint, kind
        HAVING count >= ?
        ORDER BY count DESC, last_id DESC
        LIMIT 5
        """,
        (*params, threshold),
    ).fetchall()
    for row in rows:
        last = db.execute("SELECT summary, category FROM events WHERE id = ?", (row["last_id"],)).fetchone()
        summary = str(last["summary"] if last else "")
        category = str(last["category"] if last else "repeat")
        if category not in REPEAT_WARNING_CATEGORIES:
            continue
        warnings.append(f"{warning_label(category)} repeated {row['count']} times: {summary[:220]}")
    return warnings[:5]


def warning_label(category: str) -> str:
    labels = {
        "tool_call": "Same command loop",
        "tool_failure": "Same failure loop",
        "stale_restart_signal": "Stale restart signal",
        "oscillation_or_revert_risk": "Step regression or oscillation risk",
        "completion_claim": "Completion claim",
        "investigation": "Investigation loop",
        "permission_request": "Repeated permission request",
    }
    return labels.get(category, category.replace("_", " ").title())


def failure_loop_warnings(
    db: sqlite3.Connection, project: Project, *, threshold: int = DEFAULT_LOOP_THRESHOLD
) -> list[str]:
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ? AND event_name = 'PostToolUse'
        ORDER BY id DESC
        LIMIT 80
        """,
        (str(project.root),),
    ).fetchall()
    buckets: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        summary = str(row["summary"] or "")
        if not is_failure_summary(summary, row["outcome"]):
            continue
        sig = failure_signature(summary)
        buckets.setdefault(sig, []).append(row)
    warnings: list[str] = []
    for group in buckets.values():
        if len(group) >= threshold:
            warnings.append(f"Same failure loop repeated {len(group)} times: {group[0]['summary'][:220]}")
    return warnings


def failure_signature(summary: str) -> str:
    lines = re.split(r"(?<=[.!?])\s+|\\n", summary)
    interesting = [
        line
        for line in lines
        if re.search(r"(?i)\b(error|failed|failure|traceback|exception|fatal|exit_code|assert)\b", line)
    ]
    base = " ".join(interesting[:4]) or summary
    return hash_text(normalize_for_fingerprint(base))[:20]


def investigation_loop_warnings(
    db: sqlite3.Connection, project: Project, *, threshold: int = 4
) -> list[str]:
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ? AND category = 'investigation'
        ORDER BY id DESC
        LIMIT 24
        """,
        (str(project.root),),
    ).fetchall()
    if len(rows) < threshold:
        return []
    latest = rows[0]
    same = [row for row in rows if row["fingerprint"] == latest["fingerprint"]]
    if len(same) >= threshold:
        return [f"Investigation loop repeated {len(same)} times: {latest['summary'][:220]}"]
    return []


def tool_output_blindness_warnings(db: sqlite3.Connection, project: Project) -> list[str]:
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ?
        ORDER BY id DESC
        LIMIT 6
        """,
        (str(project.root),),
    ).fetchall()
    if len(rows) < 2:
        return []
    latest_stop = next((row for row in rows if row["event_name"] == "Stop"), None)
    latest_failure = next((row for row in rows if row["category"] == "tool_failure"), None)
    if latest_stop and latest_failure and int(latest_stop["id"]) > int(latest_failure["id"]):
        if looks_complete(str(latest_stop["summary"] or "")):
            return [
                "Tool-output blindness risk: the latest assistant message claims completion after a failing tool result."
            ]
    return []


def dedupe_keep_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def compact_lines(lines: Iterable[str], max_chars: int) -> str:
    marker = "...[packet truncated to stay compact]"
    out: list[str] = []
    used = 0
    for line in lines:
        line = line.rstrip()
        cost = len(line) + 1
        if used + cost > max_chars:
            marker_cost = len(marker) + 1
            while out and used + marker_cost > max_chars:
                removed = out.pop()
                used -= len(removed) + 1
            if used + marker_cost <= max_chars:
                out.append(marker)
            break
        out.append(line)
        used += cost
    return "\n".join(out).strip()


def escape_packet_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .strip()
    )


def bullet_lines(text: str | None, *, fallback: str | None = None) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return [f"- {fallback}"] if fallback else []
    lines: list[str] = []
    for part in raw.splitlines():
        item = part.strip()
        if not item:
            continue
        item = item[2:].strip() if item.startswith("- ") else item
        lines.append(f"- {escape_packet_text(item)}")
    return lines


def trim_packet_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", escape_packet_text(value)).strip()
    if len(text) <= limit:
        return text
    if limit <= 14:
        return text[:limit]
    return text[: limit - 14].rstrip() + " ...[cut]"


def first_packet_line(text: str | None, *, fallback: str, limit: int) -> str:
    raw = str(text or "").strip()
    if not raw:
        return trim_packet_text(fallback, limit)
    for part in raw.splitlines():
        item = part.strip()
        if item:
            item = item[2:].strip() if item.startswith("- ") else item
            return trim_packet_text(item, limit)
    return trim_packet_text(fallback, limit)


def strongest_evidence_line(checkpoint: sqlite3.Row | None, *, limit: int) -> str:
    if not checkpoint:
        return "No verification evidence recorded yet."
    for field in ("tests_passed", "evidence", "commands_run", "files_touched"):
        value = first_packet_line(checkpoint[field], fallback="", limit=limit)
        if value:
            return value
    return "No verification evidence recorded yet."


def strongest_blocker_line(checkpoint: sqlite3.Row | None, *, limit: int) -> str:
    if not checkpoint:
        return "None recorded."
    for field in ("tests_failed", "blockers"):
        value = first_packet_line(checkpoint[field], fallback="", limit=limit)
        if value:
            return value
    return "None recorded."


def strongest_do_not_repeat_line(
    checkpoint: sqlite3.Row | None,
    warnings: list[str],
    *,
    limit: int,
) -> str:
    items: list[str] = []
    if checkpoint:
        value = first_packet_line(checkpoint["do_not_repeat"], fallback="", limit=max(limit, 40))
        if value:
            items.append(value)
    if warnings:
        items.append(trim_packet_text(warnings[0], max(limit, 40)))
    if not items:
        items.append("No repeat warning recorded.")
    return trim_packet_text(" | ".join(items), limit)


def is_active_checkpoint_row(checkpoint: sqlite3.Row | None) -> bool:
    return bool(checkpoint and checkpoint["status"] in {"active", "blocked"})


def priority_resume_packet(
    *,
    checkpoint: sqlite3.Row | None,
    last_checkpoint: sqlite3.Row | None = None,
    warnings: list[str],
    reason: str,
    max_chars: int,
) -> str:
    objective = (
        f"status: {trim_packet_text(checkpoint['status'], 16)}; objective: {trim_packet_text(checkpoint['objective'], 72)}"
        if checkpoint
        else "status: none; objective: No checkpoint recorded yet."
    )
    next_action = (
        trim_packet_text(checkpoint["next_action"] or "Verify current files, then continue the active objective.", 82)
        if checkpoint
        else "Infer the live objective and save a checkpoint."
    )
    blocker = strongest_blocker_line(checkpoint, limit=72)
    evidence = strongest_evidence_line(checkpoint, limit=72)
    avoid = strongest_do_not_repeat_line(checkpoint, warnings, limit=84)
    lines = [
        f'<compaction-sentinel version="{VERSION}" schema="packet-v2" reason="{trim_packet_text(reason, 32)}">',
        f"<active_objective>{objective}</active_objective>",
    ]
    if not checkpoint and last_checkpoint:
        lines.append(
            "<last_checkpoint>"
            "historical only; "
            f"status: {trim_packet_text(last_checkpoint['status'], 16)}; "
            f"objective: {trim_packet_text(last_checkpoint['objective'], 72)}"
            "</last_checkpoint>"
        )
    lines.extend(
        [
            f"<next_action>{next_action}</next_action>",
            f"<blockers>- {blocker}</blockers>",
            f"<evidence>- {evidence}</evidence>",
            f"<do_not_repeat>- {avoid}</do_not_repeat>",
            "<resume_contract>Continue live objective; do not restart; verify latest output before completion.</resume_contract>",
            "</compaction-sentinel>",
        ]
    )
    packet = "\n".join(lines)
    if len(packet) <= max_chars:
        return packet
    for limit in (56, 44, 32, 24):
        objective = (
            f"status: {trim_packet_text(checkpoint['status'], 10)}; objective: {trim_packet_text(checkpoint['objective'], limit)}"
            if checkpoint
            else "status: none; objective: No checkpoint."
        )
        lines = [
            f'<compaction-sentinel version="{VERSION}" schema="packet-v2" reason="{trim_packet_text(reason, 20)}">',
            f"<active_objective>{objective}</active_objective>",
        ]
        if not checkpoint and last_checkpoint:
            lines.append(
                "<last_checkpoint>"
                "historical only; "
                f"status: {trim_packet_text(last_checkpoint['status'], 10)}; "
                f"objective: {trim_packet_text(last_checkpoint['objective'], limit)}"
                "</last_checkpoint>"
            )
        lines.extend(
            [
                f"<next_action>{trim_packet_text(next_action, limit)}</next_action>",
                f"<blockers>- {strongest_blocker_line(checkpoint, limit=limit)}</blockers>",
                f"<evidence>- {strongest_evidence_line(checkpoint, limit=limit)}</evidence>",
                f"<do_not_repeat>- {strongest_do_not_repeat_line(checkpoint, warnings, limit=limit)}</do_not_repeat>",
                "<resume_contract>Continue live work; do not restart; verify before completion.</resume_contract>",
                "</compaction-sentinel>",
            ]
        )
        packet = "\n".join(lines)
        if len(packet) <= max_chars:
            return packet
    return compact_lines(lines, max_chars=max_chars)


def build_resume_packet(
    db: sqlite3.Connection,
    project: Project,
    *,
    reason: str = "resume",
    max_chars: int = DEFAULT_MAX_PACKET_CHARS,
    loop_threshold: int = DEFAULT_LOOP_THRESHOLD,
) -> str:
    checkpoint = active_checkpoint(db, project)
    latest = latest_checkpoint(db, project)
    last_checkpoint = latest if not checkpoint and not is_active_checkpoint_row(latest) else None
    events = recent_events(db, project)
    notes = recent_notes(db, project)
    warnings = loop_warnings(db, project, threshold=loop_threshold)
    generated_at = utc_now().replace("+00:00", "Z")

    lines: list[str] = [
        f'<compaction-sentinel version="{VERSION}" schema="packet-v2" reason="{escape_packet_text(reason)}" generated_at="{generated_at}">',
        "<authority>",
        "1. Current files and verified command results.",
        "2. Active checkpoint.",
        "3. User acceptance criteria.",
        "4. Recent event trail.",
        "</authority>",
        "",
        "<project>",
        f"name: {escape_packet_text(project.name)}",
        f"root: {escape_packet_text(project.root)}",
        f"host: {escape_packet_text(platform.system() + ' ' + platform.release())}",
        "</project>",
    ]

    if checkpoint:
        lines.extend(
            [
                "",
                "<active_objective>",
                f"status: {escape_packet_text(checkpoint['status'])}",
                f"objective: {escape_packet_text(checkpoint['objective'])}",
                f"confidence: {escape_packet_text(checkpoint['confidence'] or 'unspecified')}",
                "</active_objective>",
            ]
        )
        lines.append("")
        lines.append("<acceptance_criteria>")
        lines.extend(bullet_lines(checkpoint["acceptance_criteria"], fallback="Preserve the user's latest natural-language finish line."))
        lines.append("</acceptance_criteria>")

        lines.append("")
        lines.append("<current_state>")
        lines.extend(bullet_lines(checkpoint["current_step"], fallback="Verify current files, then continue."))
        if checkpoint["decisions_made"]:
            lines.extend(bullet_lines(checkpoint["decisions_made"]))
        if checkpoint["assumptions"]:
            lines.append("- Assumptions:")
            lines.extend(bullet_lines(checkpoint["assumptions"]))
        lines.append("</current_state>")

        lines.append("")
        lines.append("<next_action>")
        lines.append(escape_packet_text(checkpoint["next_action"] or "Verify current files, then continue the active objective."))
        lines.append("</next_action>")

        lines.append("")
        lines.append("<blockers>")
        lines.extend(bullet_lines(checkpoint["blockers"], fallback="None recorded."))
        if checkpoint["tests_failed"]:
            lines.extend(bullet_lines(checkpoint["tests_failed"]))
        lines.append("</blockers>")

        lines.append("")
        lines.append("<evidence>")
        if checkpoint["evidence"]:
            lines.extend(bullet_lines(checkpoint["evidence"]))
        if checkpoint["tests_passed"]:
            lines.extend(bullet_lines(checkpoint["tests_passed"]))
        if checkpoint["commands_run"]:
            lines.append("- Commands run:")
            lines.extend(bullet_lines(checkpoint["commands_run"]))
        if checkpoint["files_touched"]:
            lines.append("- Files touched:")
            lines.extend(bullet_lines(checkpoint["files_touched"]))
        if checkpoint["last_verified_at"]:
            lines.append(f"- last_verified_at: {escape_packet_text(checkpoint['last_verified_at'])}")
        if not any(checkpoint[field] for field in ("evidence", "tests_passed", "commands_run", "files_touched")):
            lines.append("- No verification evidence recorded yet.")
        lines.append("</evidence>")
    else:
        lines.extend(
            [
                "",
                "<active_objective>",
                "status: none",
                "objective: No checkpoint recorded yet.",
                "</active_objective>",
                "",
                "<next_action>",
                "Infer the live objective from the newest user message and save a checkpoint once it is clear.",
                "</next_action>",
            ]
        )

    if last_checkpoint:
        lines.extend(
            [
                "",
                "<last_checkpoint>",
                f"status: {escape_packet_text(last_checkpoint['status'])}",
                f"objective: {escape_packet_text(last_checkpoint['objective'])}",
                f"completed_at: {escape_packet_text(last_checkpoint['completed_at'] or last_checkpoint['updated_at'])}",
            ]
        )
        current_step = first_packet_line(last_checkpoint["current_step"], fallback="", limit=500)
        if current_step:
            lines.append(f"current_step: {current_step}")
        evidence = first_packet_line(
            last_checkpoint["tests_passed"] or last_checkpoint["evidence"],
            fallback="",
            limit=500,
        )
        if evidence:
            lines.append(f"evidence: {evidence}")
        lines.append("note: historical context only; no active checkpoint is currently recorded.")
        lines.append("</last_checkpoint>")

    if warnings or (checkpoint and checkpoint["do_not_repeat"]):
        lines.append("")
        lines.append("<do_not_repeat>")
        if checkpoint and checkpoint["do_not_repeat"]:
            lines.extend(bullet_lines(checkpoint["do_not_repeat"]))
        for warning in warnings:
            lines.append(f"- {escape_packet_text(warning)}")
        lines.append("- Before repeating, inspect the concrete artifact/log/result and choose one new hypothesis.")
        lines.append("</do_not_repeat>")

    if notes:
        lines.append("")
        lines.append("<continuity_notes>")
        for note in notes:
            suffix = f" when {note['surface_condition']}" if note["surface_condition"] else ""
            lines.append(f"- {escape_packet_text(note['content'])}{escape_packet_text(suffix)}")
        lines.append("</continuity_notes>")

    if events:
        lines.append("")
        lines.append("<recent_event_trail>")
        for event in events:
            time = str(event["created_at"]).replace("+00:00", "Z")
            outcome = f" outcome={event['outcome']}" if event["outcome"] else ""
            lines.append(
                f"- {escape_packet_text(time)} {escape_packet_text(event['event_name'])}/{escape_packet_text(event['kind'])}{escape_packet_text(outcome)}: {escape_packet_text(event['summary'])}"
            )
        lines.append("</recent_event_trail>")

    lines.extend(
        [
            "",
            "<resume_contract>",
            "- Continue the live objective from this packet; do not restart from only the last user message.",
            "- Treat checkpoints, notes, current files, and verified command output as authority over stale summaries.",
            "- Preserve user acceptance criteria in natural language.",
            "- If a warning appears, inspect the latest concrete evidence and change hypothesis before repeating.",
            "- Update Compaction Sentinel when current step, next action, blocker, evidence, or do-not-repeat state changes.",
            "</resume_contract>",
            "</compaction-sentinel>",
        ]
    )
    if max_chars <= 2500:
        return priority_resume_packet(
            checkpoint=checkpoint,
            last_checkpoint=last_checkpoint,
            warnings=warnings,
            reason=reason,
            max_chars=max_chars,
        )
    return compact_lines(lines, max_chars=max_chars)


def hook_output(event_name: str, context: str | None = None, system_message: str | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {}
    if system_message:
        output["systemMessage"] = system_message
    if context:
        output["hookSpecificOutput"] = {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    return output


def get_state(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_state(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utc_now()),
    )
    db.commit()


def project_state_id(project: Project) -> str:
    return hash_text(str(project.root.resolve()))[:24]


def project_state_prefix(project: Project) -> str:
    return f"project:{project_state_id(project)}:"


def turn_state_id(payload: dict[str, Any]) -> str:
    session_id = str(payload.get("session_id") or "session")
    turn_id = str(payload.get("turn_id") or "turn")
    return hash_text(f"{session_id}:{turn_id}")[:24]


def stop_continue_turn_key(project: Project, payload: dict[str, Any]) -> str:
    return f"{project_state_prefix(project)}stop_continue:turn:{turn_state_id(payload)}"


def stop_continue_checkpoint_key(project: Project, payload: dict[str, Any], checkpoint: sqlite3.Row) -> str:
    return f"{project_state_prefix(project)}stop_continue:checkpoint:{turn_state_id(payload)}:{checkpoint['id']}"


def stop_continue_last_signature_key(project: Project) -> str:
    return f"{project_state_prefix(project)}stop_continue:last_signature"


def stop_continue_next_action_key(project: Project, checkpoint: sqlite3.Row) -> str:
    return f"{project_state_prefix(project)}stop_continue:next_action:{hash_text(str(checkpoint['next_action'] or ''))[:16]}"


def stop_continue_cooldown_key(project: Project) -> str:
    return f"{project_state_prefix(project)}stop_continue:last_at"


def stop_signature(checkpoint: sqlite3.Row) -> str:
    return hash_text(
        "|".join(
            [
                str(checkpoint["id"]),
                str(checkpoint["updated_at"]),
                str(checkpoint["next_action"] or ""),
            ]
        )
    )[:24]


def state_count(db: sqlite3.Connection, key: str) -> int:
    try:
        return int(get_state(db, key) or "0")
    except ValueError:
        return 0


def within_cooldown(last_at: str | None, seconds: int) -> bool:
    if not last_at or seconds <= 0:
        return False
    try:
        timestamp = dt.datetime.fromisoformat(last_at.replace("Z", "+00:00"))
    except Exception:
        return False
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return (now - timestamp).total_seconds() < seconds


def maybe_stop_continue(
    db: sqlite3.Connection,
    project: Project,
    payload: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    policy = str(config.get("auto_continue") or "off").lower()
    if policy not in {"gentle", "strict"}:
        return None
    if payload.get("stop_hook_active"):
        return None
    checkpoint = active_checkpoint(db, project)
    if not checkpoint or checkpoint["status"] == "complete":
        return None
    max_per_turn = config_int(config, "stop_continue_max_per_turn", 1, minimum=0)
    max_per_checkpoint = config_int(config, "stop_continue_max_per_checkpoint_per_turn", 1, minimum=0)
    if max_per_turn <= 0:
        return None
    turn_key = stop_continue_turn_key(project, payload)
    checkpoint_key = stop_continue_checkpoint_key(project, payload, checkpoint)
    if state_count(db, turn_key) >= max_per_turn:
        return None
    if max_per_checkpoint > 0 and state_count(db, checkpoint_key) >= max_per_checkpoint:
        return None
    cooldown_seconds = config_int(config, "stop_continue_cooldown_seconds", 0, minimum=0)
    if within_cooldown(get_state(db, stop_continue_cooldown_key(project)), cooldown_seconds):
        return None
    last = extract_last_assistant(payload)
    if looks_complete(last):
        return None
    signature = stop_signature(checkpoint)
    last_signature_key = stop_continue_last_signature_key(project)
    if get_state(db, last_signature_key) == signature:
        return None
    if checkpoint["next_action"]:
        next_action_key = stop_continue_next_action_key(project, checkpoint)
        if get_state(db, next_action_key) == "used":
            return None
    if loop_warnings(db, project, threshold=config_int(config, "loop_threshold", DEFAULT_LOOP_THRESHOLD, minimum=1)):
        return None
    set_state(db, turn_key, str(state_count(db, turn_key) + 1))
    set_state(db, checkpoint_key, str(state_count(db, checkpoint_key) + 1))
    set_state(db, last_signature_key, signature)
    set_state(db, stop_continue_cooldown_key(project), utc_now())
    if checkpoint["next_action"]:
        set_state(db, stop_continue_next_action_key(project, checkpoint), "used")
    reason = build_resume_packet(
        db,
        project,
        reason="stop-continuation",
        max_chars=min(config_int(config, "max_packet_chars", DEFAULT_MAX_PACKET_CHARS, minimum=500), 6500),
        loop_threshold=config_int(config, "loop_threshold", DEFAULT_LOOP_THRESHOLD, minimum=1),
    )
    reason += "\n\nContinue this active objective. First state the next concrete action, then perform it."
    if policy == "strict":
        reason += "\nStrict mode is enabled; continue only if the next action is safe and evidence-driven."
    return {"decision": "block", "reason": reason}


def should_record_pre_tool(mode: str, command: str) -> bool:
    if mode == "full":
        return True
    if mode == "light":
        return is_test_or_build_command(command)
    return True


def should_record_post_tool(mode: str, command: str, response: str, outcome: str | None) -> bool:
    if mode == "full":
        return True
    summary = f"Bash: {command}"
    if response:
        summary += f" -> {response}"
    failed = is_failure_summary(summary, outcome)
    if mode == "light":
        return failed or is_test_or_build_command(command)
    return True


def pre_tool_warnings(
    db: sqlite3.Connection,
    project: Project,
    row: sqlite3.Row,
    *,
    threshold: int,
    mode: str,
) -> list[str]:
    if mode == "light":
        return []
    return repeat_warnings(db, project, fingerprint_value=row["fingerprint"], threshold=threshold)


def post_tool_warnings(
    db: sqlite3.Connection,
    project: Project,
    row: sqlite3.Row,
    *,
    command: str,
    threshold: int,
    mode: str,
) -> list[str]:
    if mode == "light":
        return []
    warnings = repeat_warnings(db, project, fingerprint_value=row["fingerprint"], threshold=threshold)
    category = str(row["category"] or "")
    if category == "tool_failure" or is_test_or_build_command(command):
        warnings.extend(failure_loop_warnings(db, project, threshold=threshold))
    return dedupe_keep_order(warnings)[:8]


def handle_hook(event_name: str, payload: dict[str, Any], codex_home: Path | None = None) -> dict[str, Any]:
    db = connect(codex_home)
    try:
        return _handle_hook(event_name, payload, db, codex_home)
    finally:
        db.close()


def _handle_hook(
    event_name: str,
    payload: dict[str, Any],
    db: sqlite3.Connection,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    config = load_runtime_config(codex_home)
    redact = bool(config.get("redact", True))
    project = project_from_payload(payload)
    event_name = event_name or str(payload.get("hook_event_name") or "")
    loop_threshold = config_int(config, "loop_threshold", DEFAULT_LOOP_THRESHOLD, minimum=1)
    max_chars = config_int(config, "max_packet_chars", DEFAULT_MAX_PACKET_CHARS, minimum=500)
    max_events = config_int(config, "max_events_per_project", DEFAULT_MAX_EVENTS_PER_PROJECT, minimum=0)
    retention_days = config_int(config, "retention_days", DEFAULT_RETENTION_DAYS, minimum=0)
    retention_interval = config_int(
        config,
        "retention_check_interval_seconds",
        DEFAULT_RETENTION_CHECK_INTERVAL_SECONDS,
        minimum=0,
    )
    prune_interval = config_int(
        config,
        "prune_check_interval_events",
        DEFAULT_PRUNE_CHECK_INTERVAL_EVENTS,
        minimum=1,
    )
    input_limit = config_int(config, "max_tool_input_chars", DEFAULT_MAX_TOOL_INPUT_CHARS, minimum=120)
    mode = performance_mode(config)

    if event_name == "UserPromptSubmit":
        prompt = extract_prompt(payload, redact=redact)
        row = record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind="prompt",
            summary=prompt or "User prompt submitted.",
            details={"prompt": prompt},
            redact=redact,
            max_events=max_events,
            retention_days=retention_days,
            retention_check_interval_seconds=retention_interval,
            prune_check_interval_events=prune_interval,
        )
        update_checkpoint_from_prompt(db, project, payload, prompt, redact=redact)
        packet = build_resume_packet(
            db,
            project,
            reason="user-prompt",
            max_chars=max_chars,
            loop_threshold=loop_threshold,
        )
        warnings = loop_warnings(db, project, fingerprint_value=row["fingerprint"], threshold=loop_threshold)
        message = (
            "Compaction Sentinel detected repeated context; inspect the latest artifact before repeating."
            if warnings
            else None
        )
        return hook_output(event_name, packet, message)

    if event_name == "SessionStart":
        record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind="session",
            summary=f"Session start: {payload.get('source') or 'unknown'}",
            details={"source": payload.get("source")},
            redact=redact,
            max_events=max_events,
            retention_days=retention_days,
            retention_check_interval_seconds=retention_interval,
            prune_check_interval_events=prune_interval,
        )
        packet = build_resume_packet(
            db,
            project,
            reason="session-start",
            max_chars=max_chars,
            loop_threshold=loop_threshold,
        )
        return hook_output(event_name, packet)

    if event_name == "PreToolUse":
        tool_name = extract_tool_name(payload)
        tool_input = extract_tool_input(payload, redact=redact, limit=input_limit)
        if not should_record_pre_tool(mode, tool_input):
            return {}
        row = record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind=f"tool:{tool_name}",
            summary=tool_input or f"{tool_name} invoked",
            details={"tool_name": tool_name, "tool_input": tool_input},
            redact=redact,
            max_events=max_events,
            retention_days=retention_days,
            retention_check_interval_seconds=retention_interval,
            prune_check_interval_events=prune_interval,
        )
        warnings = pre_tool_warnings(db, project, row, threshold=loop_threshold, mode=mode)
        if warnings:
            return hook_output(
                event_name,
                "Compaction Sentinel loop warning:\n"
                + "\n".join(f"- {warning}" for warning in warnings)
                + "\nBefore repeating this tool call, inspect the newest concrete evidence and change the hypothesis.",
                "Repeated tool pattern detected.",
            )
        return {}

    if event_name == "PermissionRequest":
        tool_name = extract_tool_name(payload)
        tool_input = extract_tool_input(payload, redact=redact, limit=input_limit)
        reason = extract_permission_reason(payload, redact=redact)
        checkpoint = active_checkpoint(db, project)
        summary = tool_input or f"{tool_name} permission requested"
        if reason:
            summary += f" | reason: {reason}"
        row = record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind=f"permission:{tool_name}",
            summary=summary,
            details={
                "tool_name": tool_name,
                "tool_input": tool_input,
                "approval_reason": reason,
                "active_checkpoint": checkpoint_to_dict(checkpoint),
            },
            redact=redact,
            max_events=max_events,
            retention_days=retention_days,
            retention_check_interval_seconds=retention_interval,
            prune_check_interval_events=prune_interval,
        )
        warnings = repeat_warnings(db, project, fingerprint_value=row["fingerprint"], threshold=loop_threshold)
        if warnings:
            return {
                "systemMessage": "Repeated permission request detected. Compaction Sentinel recorded it but will not approve or deny it automatically."
            }
        return {}

    if event_name == "PostToolUse":
        tool_name = extract_tool_name(payload)
        tool_input = extract_tool_input(payload, redact=redact, limit=input_limit)
        response, outcome = extract_tool_response(payload, redact=redact, command=tool_input, config=config)
        if not should_record_post_tool(mode, tool_input, response, outcome):
            return {}
        summary = f"{tool_name}: {tool_input}"
        if response:
            summary += f" -> {response}"
        row = record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind=f"tool-result:{tool_name}",
            summary=summary,
            details={"tool_name": tool_name, "tool_input": tool_input, "tool_response": response},
            outcome=outcome,
            redact=redact,
            max_events=max_events,
            retention_days=retention_days,
            retention_check_interval_seconds=retention_interval,
            prune_check_interval_events=prune_interval,
        )
        warnings = post_tool_warnings(
            db,
            project,
            row,
            command=tool_input,
            threshold=loop_threshold,
            mode=mode,
        )
        if warnings:
            return hook_output(
                event_name,
                "Compaction Sentinel loop warning after tool result:\n"
                + "\n".join(f"- {warning}" for warning in warnings)
                + "\nUse the latest output as evidence; do not rerun the same step blindly.",
                "Repeated post-tool result pattern detected.",
            )
        return {}

    if event_name == "Stop":
        last = extract_last_assistant(payload, redact=redact)
        record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind="stop",
            summary=last or "Turn stopped.",
            details={"last_assistant_message": last},
            redact=redact,
            max_events=max_events,
            retention_days=retention_days,
            retention_check_interval_seconds=retention_interval,
            prune_check_interval_events=prune_interval,
        )
        warnings = loop_warnings(db, project, threshold=loop_threshold)
        continuation = maybe_stop_continue(db, project, payload, config)
        if continuation:
            return continuation
        if warnings:
            return hook_output(
                event_name,
                "Compaction Sentinel stop warning:\n"
                + "\n".join(f"- {warning}" for warning in warnings)
                + "\nInspect the latest concrete output before claiming completion.",
                "Compaction Sentinel detected a possible stop-time regression.",
            )
        return {}

    record_event(
        db,
        project,
        payload,
        event_name=event_name or "Unknown",
        kind="unknown",
        summary=f"Unhandled hook event: {event_name}",
        details=payload,
        redact=redact,
        max_events=max_events,
        retention_days=retention_days,
        retention_check_interval_seconds=retention_interval,
        prune_check_interval_events=prune_interval,
    )
    return {}


def search_events(
    db: sqlite3.Connection, project: Project, query: str, *, limit: int = 8
) -> list[sqlite3.Row]:
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_./:-]{2,}", query)]
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ?
        ORDER BY id DESC
        LIMIT 200
        """,
        (str(project.root),),
    ).fetchall()
    if not terms:
        return rows[:limit]
    scored: list[tuple[int, sqlite3.Row]] = []
    for row in rows:
        haystack = f"{row['summary']} {row['details_json']} {row['event_name']} {row['kind']} {row['category']}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
    return [row for _, row in scored[:limit]]


def project_from_cli(cwd: str | None = None) -> Project:
    root = find_project_root(Path(cwd or os.getcwd()))
    return Project(root=root, name=root.name or "workspace")


def scrub_project(db: sqlite3.Connection, project: Project) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("events", "notes", "checkpoints"):
        row = db.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE project_root = ?",
            (str(project.root),),
        ).fetchone()
        counts[table] = int(row["count"] if row else 0)
        db.execute(f"DELETE FROM {table} WHERE project_root = ?", (str(project.root),))
    state_keys = {
        str(row["key"])
        for row in db.execute(
            "SELECT key FROM state WHERE key LIKE ?",
            (project_state_prefix(project) + "%",),
        ).fetchall()
    }
    # Remove legacy v0.3 keys that stored the raw project path.
    raw_root = str(project.root)
    state_keys.update(
        str(row["key"])
        for row in db.execute("SELECT key FROM state").fetchall()
        if raw_root in str(row["key"])
    )
    counts["state"] = len(state_keys)
    for key in state_keys:
        db.execute("DELETE FROM state WHERE key = ?", (key,))
    db.commit()
    return counts


def scrub_all(db: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("events", "notes", "checkpoints", "state"):
        row = db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        counts[table] = int(row["count"] if row else 0)
        db.execute(f"DELETE FROM {table}")
    db.commit()
    return counts


def export_project(db: sqlite3.Connection, project: Project) -> dict[str, Any]:
    checkpoints = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM checkpoints WHERE project_root = ? ORDER BY id DESC",
            (str(project.root),),
        ).fetchall()
    ]
    notes = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM notes WHERE project_root = ? ORDER BY id DESC",
            (str(project.root),),
        ).fetchall()
    ]
    events = []
    for row in db.execute(
        "SELECT * FROM events WHERE project_root = ? ORDER BY id DESC",
        (str(project.root),),
    ).fetchall():
        item = dict(row)
        try:
            item["details"] = json.loads(str(item.pop("details_json") or "{}"))
        except Exception:
            item["details"] = {}
        events.append(item)
    return {
        "version": VERSION,
        "exported_at": utc_now(),
        "project": project.name,
        "project_root": str(project.root),
        "checkpoints": checkpoints,
        "notes": notes,
        "events": events,
    }
