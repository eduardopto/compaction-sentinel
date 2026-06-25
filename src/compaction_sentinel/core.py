"""Core storage, hook handling, and resume-packet logic for Compaction Sentinel."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import shlex
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


VERSION = "0.5.0"
SCHEMA_USER_VERSION = 500
APP_NAME = "Compaction Sentinel"
DEFAULT_STREAM_ID = "default"
DEFAULT_SOURCE_SYSTEM = "compaction-sentinel"
MAX_STREAM_ID_CHARS = 64
MAX_STREAM_LABEL_CHARS = 120
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
    "stream_id": "TEXT NOT NULL DEFAULT 'default'",
    "stream_label": "TEXT",
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
    "compaction_epoch": "INTEGER NOT NULL DEFAULT 0",
    "source_system": "TEXT",
    "source_ref": "TEXT",
    "foreign_project_hint": "TEXT",
    "quarantine_reason": "TEXT",
    "thread_id": "TEXT",
}

EVENT_EXTRA_COLUMNS = {
    "stream_id": "TEXT NOT NULL DEFAULT 'default'",
    "stream_label": "TEXT",
    "tool_use_id": "TEXT",
    "event_key": "TEXT",
    "category": "TEXT",
    "compaction_epoch": "INTEGER NOT NULL DEFAULT 0",
    "source_system": "TEXT",
    "source_ref": "TEXT",
    "foreign_project_hint": "TEXT",
    "quarantine_reason": "TEXT",
    "thread_id": "TEXT",
}

NOTE_EXTRA_COLUMNS = {
    "stream_id": "TEXT NOT NULL DEFAULT 'default'",
    "stream_label": "TEXT",
    "compaction_epoch": "INTEGER NOT NULL DEFAULT 0",
    "source_system": "TEXT",
    "source_ref": "TEXT",
    "foreign_project_hint": "TEXT",
    "quarantine_reason": "TEXT",
    "thread_id": "TEXT",
}


@dataclass(frozen=True)
class Project:
    root: Path
    name: str


@dataclass(frozen=True)
class StreamScope:
    id: str = DEFAULT_STREAM_ID
    label: str = ""


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
        "compact_hooks_capture_only": True,
        "compact_context_smoke_passed": False,
        "compact_resume_injection": False,
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
          stream_id TEXT NOT NULL DEFAULT 'default',
          stream_label TEXT,
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
          compaction_epoch INTEGER NOT NULL DEFAULT 0,
          source_system TEXT,
          source_ref TEXT,
          foreign_project_hint TEXT,
          quarantine_reason TEXT,
          thread_id TEXT,
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
          stream_id TEXT NOT NULL DEFAULT 'default',
          stream_label TEXT,
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
          compaction_epoch INTEGER NOT NULL DEFAULT 0,
          source_system TEXT,
          source_ref TEXT,
          foreign_project_hint TEXT,
          quarantine_reason TEXT,
          thread_id TEXT,
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
          stream_id TEXT NOT NULL DEFAULT 'default',
          stream_label TEXT,
          status TEXT NOT NULL DEFAULT 'open',
          content TEXT NOT NULL,
          surface_condition TEXT,
          compaction_epoch INTEGER NOT NULL DEFAULT 0,
          source_system TEXT,
          source_ref TEXT,
          foreign_project_hint TEXT,
          quarantine_reason TEXT,
          thread_id TEXT,
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
        CREATE TABLE IF NOT EXISTS memory_candidates (
          id INTEGER PRIMARY KEY,
          project_root TEXT NOT NULL,
          project_name TEXT NOT NULL,
          stream_id TEXT NOT NULL DEFAULT 'default',
          stream_label TEXT,
          content TEXT NOT NULL,
          title TEXT,
          tags TEXT,
          importance TEXT,
          source_system TEXT,
          source_ref TEXT,
          foreign_project_hint TEXT,
          quarantine_reason TEXT,
          thread_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    migrate_columns(db, "events", EVENT_EXTRA_COLUMNS)
    checkpoint_columns = {"completed_at": "TEXT", **CHECKPOINT_EXTRA_COLUMNS}
    migrate_columns(db, "checkpoints", checkpoint_columns)
    migrate_columns(db, "notes", NOTE_EXTRA_COLUMNS)
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_events_project_stream_id
          ON events(project_root, stream_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_checkpoints_project_stream_status
          ON checkpoints(project_root, stream_id, status, id DESC);
        CREATE INDEX IF NOT EXISTS idx_notes_project_stream_status
          ON notes(project_root, stream_id, status, id DESC);
        DROP INDEX IF EXISTS idx_events_project_event_key;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_project_stream_event_key
          ON events(project_root, stream_id, event_key)
          WHERE event_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_project_stream_category_epoch_quarantine
          ON events(project_root, stream_id, category, compaction_epoch, quarantine_reason, id DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_source_ref
          ON events(source_system, source_ref)
          WHERE source_system IS NOT NULL AND source_ref IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_checkpoints_project_stream_active_quarantine
          ON checkpoints(project_root, stream_id, status, quarantine_reason, id DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_checkpoints_source_ref
          ON checkpoints(source_system, source_ref)
          WHERE source_system IS NOT NULL AND source_ref IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_notes_project_stream_quarantine
          ON notes(project_root, stream_id, status, quarantine_reason, id DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_source_ref
          ON notes(source_system, source_ref)
          WHERE source_system IS NOT NULL AND source_ref IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_memory_candidates_project_quarantine
          ON memory_candidates(project_root, quarantine_reason, id DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_candidates_source_ref
          ON memory_candidates(source_system, source_ref)
          WHERE source_system IS NOT NULL AND source_ref IS NOT NULL;
        """
    )
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_project_stream_event_key
          ON events(project_root, stream_id, event_key)
          WHERE event_key IS NOT NULL
        """
    )
    migrate_event_categories(db)
    db.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")
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


def normalize_stream_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_STREAM_ID
    slug = re.sub(r"[^A-Za-z0-9:._-]+", "-", raw).strip(":._-").lower()
    if not slug:
        return "stream-" + hash_text(raw)[:16]
    if len(slug) <= MAX_STREAM_ID_CHARS:
        return slug
    digest = hash_text(raw)[:16]
    prefix = slug[: MAX_STREAM_ID_CHARS - len(digest) - 1].strip("._-") or "stream"
    return f"{prefix}-{digest}"


def normalize_stream_label(value: Any, *, redact: bool = True) -> str:
    return normalize_text(value, MAX_STREAM_LABEL_CHARS, redact=redact)


def coerce_stream(
    stream_id: Any = None,
    stream_label: Any = "",
    *,
    redact: bool = True,
) -> StreamScope:
    return StreamScope(
        id=normalize_stream_id(stream_id),
        label=normalize_stream_label(stream_label, redact=redact),
    )


def session_id_from_payload(payload: dict[str, Any]) -> str:
    value = payload.get("session_id")
    if isinstance(value, str) and value.strip():
        return normalize_text(value, 200, redact=False)
    return ""


def thread_id_from_payload(payload: dict[str, Any]) -> str:
    for key in ("thread_id", "codex_thread_id", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value, 200, redact=False)
    return ""


def transcript_path_from_payload(payload: dict[str, Any]) -> str:
    for key in ("transcript_path", "conversation_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value, 400, redact=False)
    transcript = payload.get("transcript")
    if isinstance(transcript, dict):
        value = transcript.get("path")
        if isinstance(value, str) and value.strip():
            return normalize_text(value, 400, redact=False)
    return ""


def ambient_codex_session_id() -> str:
    for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_AGENT_ID"):
        value = os.environ.get(key)
        if value:
            return normalize_text(value, 200, redact=False)
    return ""


def stream_session_key(project: Project, session_id: str) -> str:
    return f"{project_state_prefix(project)}session_stream:{hash_text(session_id)[:24]}"


def stream_transcript_key(project: Project, transcript_path: str) -> str:
    return f"{project_state_prefix(project)}transcript_stream:{hash_text(transcript_path)[:24]}"


def stream_label_key(project: Project, stream_id: str) -> str:
    return f"{stream_state_prefix(project, stream_id)}label"


def stream_owner_key(project: Project, stream_id: str) -> str:
    return f"{stream_state_prefix(project, stream_id)}owner_session"


def stream_state_prefix(project: Project, stream_id: str) -> str:
    return f"{project_state_prefix(project)}stream:{normalize_stream_id(stream_id)}:"


def remember_stream(
    db: sqlite3.Connection,
    project: Project,
    stream: StreamScope,
    *,
    session_id: str = "",
    transcript_path: str = "",
) -> None:
    if session_id:
        set_state(db, stream_session_key(project, session_id), stream.id)
    if transcript_path:
        set_state(db, stream_transcript_key(project, transcript_path), stream.id)
    if stream.label:
        set_state(db, stream_label_key(project, stream.id), stream.label)


def stream_from_payload(
    payload: dict[str, Any],
    project: Project,
    config: dict[str, Any] | None = None,
    db: sqlite3.Connection | None = None,
) -> StreamScope:
    del config
    session_id = session_id_from_payload(payload)
    transcript_path = transcript_path_from_payload(payload)
    explicit = payload.get("stream_id") or payload.get("stream")
    label = payload.get("stream_label") or payload.get("label") or ""
    if explicit:
        normalized = normalize_stream_id(explicit)
        if db is not None and not label:
            label = get_state(db, stream_label_key(project, normalized)) or ""
        stream = coerce_stream(explicit, label)
        if db is not None:
            remember_stream(db, project, stream, session_id=session_id, transcript_path=transcript_path)
        return stream

    thread_id = thread_id_from_payload(payload)
    if thread_id:
        normalized = normalize_stream_id(thread_id)
        if db is not None and not label:
            label = get_state(db, stream_label_key(project, normalized)) or ""
        stream = coerce_stream(thread_id, label)
        if db is not None:
            remember_stream(db, project, stream, session_id=session_id, transcript_path=transcript_path)
        return stream

    if db is not None and session_id:
        mapped = get_state(db, stream_session_key(project, session_id))
        if mapped:
            stored_label = get_state(db, stream_label_key(project, mapped)) or label
            return coerce_stream(mapped, stored_label)

    if db is not None and transcript_path:
        mapped = get_state(db, stream_transcript_key(project, transcript_path))
        if mapped:
            stored_label = get_state(db, stream_label_key(project, mapped)) or label
            if session_id:
                remember_stream(db, project, coerce_stream(mapped, stored_label), session_id=session_id)
            return coerce_stream(mapped, stored_label)

    if session_id:
        stream = coerce_stream("session:" + hash_text(session_id)[:24], label)
        if db is not None:
            remember_stream(db, project, stream, session_id=session_id, transcript_path=transcript_path)
        return stream
    return coerce_stream(DEFAULT_STREAM_ID, label)


def stream_from_cli_args(
    args: Any,
    project: Project,
    config: dict[str, Any] | None = None,
    db: sqlite3.Connection | None = None,
) -> StreamScope:
    payload = {
        "stream_id": getattr(args, "stream", None),
        "stream_label": getattr(args, "stream_label", ""),
        "session_id": ambient_codex_session_id(),
    }
    return stream_from_payload(payload, project, config, db)


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
    checkout_markers = (".git",)
    for candidate in [current, *current.parents]:
        if any((candidate / marker).exists() for marker in checkout_markers):
            return candidate
    fallback_markers = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", ".codex", ".agents")
    for candidate in [current, *current.parents]:
        if any((candidate / marker).exists() for marker in fallback_markers):
            return candidate
    return current


def project_from_payload(payload: dict[str, Any]) -> Project:
    root = find_project_root(payload_cwd(payload))
    return Project(root=root, name=root.name or "workspace")


ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.-])/(?:Users|Volumes|private|tmp|var)/[^\s'\"`<>),;]+")
ACTIVE_WORK_RE = re.compile(
    r"(?i)\b("
    r"active\s+work|working\s+in|implement|fix|edit|modify|write|create|delete|remove|"
    r"apply_patch|commit|push|build|test|run|install|migrate|checkpoint|objective|next\s+action"
    r")\b"
)


def path_within_project(path: Path, project: Project) -> bool:
    try:
        candidate = path.expanduser().resolve(strict=False)
        root = project.root.expanduser().resolve(strict=False)
        return os.path.commonpath([str(candidate), str(root)]) == str(root)
    except Exception:
        return False


def clean_absolute_path(raw: str) -> str:
    return raw.rstrip(".,:;)]}\"'")


def absolute_paths_in_text(text: str) -> list[Path]:
    paths: list[Path] = []
    for match in ABSOLUTE_PATH_RE.findall(str(text or "")):
        cleaned = clean_absolute_path(match)
        if cleaned:
            paths.append(Path(cleaned).expanduser())
    return paths


def iter_text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_text_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_text_values(item)


def foreign_paths_from_value(value: Any, project: Project) -> list[Path]:
    paths: list[Path] = []
    for text in iter_text_values(value):
        for path in absolute_paths_in_text(text):
            if not path_within_project(path, project):
                paths.append(path)
    return paths


def foreign_path_hint(paths: Iterable[Path], project: Project) -> str:
    for path in paths:
        try:
            marker_root = find_project_root(path)
        except Exception:
            marker_root = path
        if not path_within_project(marker_root, project):
            return str(marker_root)
    return ""


def quarantine_reason_for_payload(
    project: Project,
    payload: dict[str, Any],
    *,
    summary: str = "",
    details: dict[str, Any] | None = None,
    event_name: str = "",
    imported: bool = False,
    ownership_proven: bool = False,
) -> tuple[str, str]:
    if imported and not ownership_proven:
        paths = foreign_paths_from_value(payload, project) + foreign_paths_from_value(summary, project)
        return foreign_path_hint(paths, project), "imported_owner_unproven"
    checked_payload: dict[str, Any] = {}
    for key in ("cwd", "project_path", "workspace_path", "transcript_path"):
        if key in payload:
            checked_payload[key] = payload[key]
    workspace = payload.get("workspace")
    if isinstance(workspace, dict):
        checked_payload["workspace"] = workspace
    explicit_paths = foreign_paths_from_value(checked_payload, project)
    if explicit_paths:
        return foreign_path_hint(explicit_paths, project), "foreign_project_hint"

    text_blob = " ".join([summary, stringify_for_storage(details or {}), stringify_for_storage(payload)])
    paths = foreign_paths_from_value(text_blob, project)
    if not paths:
        return "", ""
    if event_name in {"PreToolUse", "PostToolUse"}:
        command = extract_tool_input(payload, redact=False, limit=4000) or summary
        if is_read_only_command(command) and not ACTIVE_WORK_RE.search(text_blob):
            return "", ""
    inside_count = sum(1 for path in absolute_paths_in_text(text_blob) if path_within_project(path, project))
    if ACTIVE_WORK_RE.search(text_blob) or len(paths) > inside_count:
        return foreign_path_hint(paths, project), "foreign_active_work"
    return "", ""


def compact_epoch_key(project: Project, stream_id: str) -> str:
    return f"{stream_state_prefix(project, stream_id)}compaction_epoch"


def current_compaction_epoch(db: sqlite3.Connection, project: Project, stream_id: str) -> int:
    try:
        return int(get_state(db, compact_epoch_key(project, stream_id)) or "0")
    except ValueError:
        return 0


def set_compaction_epoch(db: sqlite3.Connection, project: Project, stream_id: str, epoch: int) -> int:
    value = max(0, int(epoch))
    set_state(db, compact_epoch_key(project, stream_id), str(value))
    return value


def advance_compaction_epoch(db: sqlite3.Connection, project: Project, stream_id: str) -> int:
    return set_compaction_epoch(db, project, stream_id, current_compaction_epoch(db, project, stream_id) + 1)


def compact_context_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("compact_context_smoke_passed")) and bool(config.get("compact_resume_injection"))


def table_name_checked(table: str) -> str:
    if table not in {"events", "checkpoints", "notes", "memory_candidates"}:
        raise ValueError(f"unsupported quarantine table: {table}")
    return table


def quarantine_count(
    db: sqlite3.Connection,
    project: Project | None = None,
    *,
    stream_id: str | None = None,
) -> int:
    total = 0
    for table in ("events", "checkpoints", "notes", "memory_candidates"):
        clauses = ["quarantine_reason IS NOT NULL", "quarantine_reason != ''"]
        params: list[Any] = []
        if project is not None:
            clauses.append("project_root = ?")
            params.append(str(project.root))
        if stream_id is not None and table != "memory_candidates":
            clauses.append("stream_id = ?")
            params.append(normalize_stream_id(stream_id))
        row = db.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        total += int(row["count"] if row else 0)
    return total


def list_quarantine(
    db: sqlite3.Connection,
    project: Project | None = None,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in ("checkpoints", "notes", "events", "memory_candidates"):
        clauses = ["quarantine_reason IS NOT NULL", "quarantine_reason != ''"]
        params: list[Any] = []
        if project is not None:
            clauses.append("project_root = ?")
            params.append(str(project.root))
        for row in db.execute(
            f"""
            SELECT * FROM {table}
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall():
            item = dict(row)
            item["table"] = table
            rows.append(item)
    rows.sort(key=lambda item: int(item.get("id") or 0), reverse=True)
    return rows[:limit]


def set_quarantine(
    db: sqlite3.Connection,
    table: str,
    row_id: int,
    *,
    reason: str | None,
    foreign_project_hint: str | None = None,
) -> bool:
    table = table_name_checked(table)
    result = db.execute(
        f"UPDATE {table} SET quarantine_reason = ?, foreign_project_hint = ? WHERE id = ?",
        (reason or None, foreign_project_hint or None, int(row_id)),
    )
    db.commit()
    return result.rowcount > 0


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
    if event_name in {"PreCompact", "PostCompact"}:
        return "compact"
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
    stream: StreamScope | None = None,
    compaction_epoch: int | None = None,
    source_system: str = DEFAULT_SOURCE_SYSTEM,
    source_ref: str = "",
    foreign_project_hint: str = "",
    quarantine_reason: str = "",
    thread_id: str = "",
    imported: bool = False,
    ownership_proven: bool = False,
) -> sqlite3.Row:
    stream = stream or stream_from_payload(payload, project, db=db)
    clean_summary = normalize_text(summary, 4000, redact=redact)
    clean_details = sanitize_json(details or {}, redact=redact)
    now = utc_now()
    tool_use_id, event_key = event_identity(payload, event_name)
    if event_key:
        event_key = hash_text(f"{stream.id}:{event_key}")[:40]
    fp = fingerprint(kind, clean_summary) if clean_summary else None
    category = event_category(event_name, kind, clean_summary, outcome)
    if compaction_epoch is None:
        compaction_epoch = current_compaction_epoch(db, project, stream.id)
    if not thread_id:
        thread_id = thread_id_from_payload(payload)
    if not quarantine_reason:
        detected_hint, detected_reason = quarantine_reason_for_payload(
            project,
            payload,
            summary=clean_summary,
            details=clean_details if isinstance(clean_details, dict) else {},
            event_name=event_name,
            imported=imported,
            ownership_proven=ownership_proven,
        )
        foreign_project_hint = foreign_project_hint or detected_hint
        quarantine_reason = detected_reason
    params = (
        str(project.root),
        project.name,
        stream.id,
        stream.label or None,
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
        int(compaction_epoch or 0),
        normalize_text(source_system, 120, redact=False) or None,
        normalize_text(source_ref, 300, redact=False) or None,
        normalize_text(foreign_project_hint, 500, redact=False) or None,
        normalize_text(quarantine_reason, 300, redact=False) or None,
        normalize_text(thread_id, 300, redact=False) or None,
        now,
    )
    db.execute(
        """
        INSERT OR IGNORE INTO events
          (project_root, project_name, stream_id, stream_label, session_id, turn_id, event_name, kind, category,
           summary, details_json, fingerprint, tool_use_id, event_key, outcome, compaction_epoch,
           source_system, source_ref, foreign_project_hint, quarantine_reason, thread_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params,
    )
    db.commit()
    if source_system and source_ref:
        row = db.execute(
            "SELECT * FROM events WHERE source_system = ? AND source_ref = ? ORDER BY id DESC LIMIT 1",
            (normalize_text(source_system, 120, redact=False), normalize_text(source_ref, 300, redact=False)),
        ).fetchone()
    elif event_key:
        row = db.execute(
            "SELECT * FROM events WHERE project_root = ? AND stream_id = ? AND event_key = ? ORDER BY id DESC LIMIT 1",
            (str(project.root), stream.id, event_key),
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
    stream: StreamScope | None = None,
    stream_id: str | None = None,
    stream_label: str = "",
    compaction_epoch: int | None = None,
    source_system: str = DEFAULT_SOURCE_SYSTEM,
    source_ref: str = "",
    foreign_project_hint: str = "",
    quarantine_reason: str = "",
    thread_id: str = "",
    imported: bool = False,
    ownership_proven: bool = False,
) -> int:
    stream = stream or coerce_stream(stream_id, stream_label, redact=redact)
    if session_id or stream.label:
        remember_stream(db, project, stream, session_id=session_id)
    clean_objective = normalize_text(objective, 1400, redact=redact)
    if not clean_objective:
        raise ValueError("checkpoint objective is required")
    if source_system and source_ref:
        existing = db.execute(
            "SELECT id FROM checkpoints WHERE source_system = ? AND source_ref = ? ORDER BY id DESC LIMIT 1",
            (normalize_text(source_system, 120, redact=False), normalize_text(source_ref, 300, redact=False)),
        ).fetchone()
        if existing:
            return int(existing["id"])
    now = utc_now()
    clean_status = normalize_text(status, 80, redact=redact) or "active"
    if clean_status not in {"active", "blocked", "complete", "superseded"}:
        clean_status = "active"
    if compaction_epoch is None:
        compaction_epoch = current_compaction_epoch(db, project, stream.id)
    if not thread_id:
        thread_id = normalize_text("", 1)
    close_status = "complete" if clean_status == "complete" else "superseded"
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
    if not quarantine_reason:
        detected_hint, detected_reason = quarantine_reason_for_payload(
            project,
            {"session_id": session_id, "thread_id": thread_id},
            summary=" ".join([clean_objective, *normalized_fields.values()]),
            imported=imported,
            ownership_proven=ownership_proven,
        )
        foreign_project_hint = foreign_project_hint or detected_hint
        quarantine_reason = detected_reason
    if not quarantine_reason:
        db.execute(
            """
            UPDATE checkpoints
            SET status = ?, completed_at = COALESCE(completed_at, ?), updated_at = ?
            WHERE project_root = ? AND stream_id = ? AND status IN ('active', 'blocked')
              AND (quarantine_reason IS NULL OR quarantine_reason = '')
            """,
            (close_status, now, now, str(project.root), stream.id),
        )
    if not normalized_fields["last_verified_at"] and (
        normalized_fields["evidence"] or normalized_fields["tests_passed"]
    ):
        normalized_fields["last_verified_at"] = now
    db.execute(
        """
        INSERT OR IGNORE INTO checkpoints
          (project_root, project_name, stream_id, stream_label, session_id, turn_id, status, objective,
           acceptance_criteria, current_step, next_action, blockers, evidence,
           files_touched, commands_run, tests_passed, tests_failed, decisions_made,
           assumptions, do_not_repeat, last_verified_at, confidence, compaction_epoch,
           source_system, source_ref, foreign_project_hint, quarantine_reason, thread_id, source,
           created_at, updated_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(project.root),
            project.name,
            stream.id,
            stream.label or None,
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
            int(compaction_epoch or 0),
            normalize_text(source_system, 120, redact=False) or None,
            normalize_text(source_ref, 300, redact=False) or None,
            normalize_text(foreign_project_hint, 500, redact=False) or None,
            normalize_text(quarantine_reason, 300, redact=False) or None,
            normalize_text(thread_id, 300, redact=False) or None,
            source,
            now,
            now,
            now if clean_status == "complete" else None,
        ),
    )
    db.commit()
    if source_system and source_ref:
        row = db.execute(
            "SELECT id FROM checkpoints WHERE source_system = ? AND source_ref = ? ORDER BY id DESC LIMIT 1",
            (normalize_text(source_system, 120, redact=False), normalize_text(source_ref, 300, redact=False)),
        ).fetchone()
    else:
        row = db.execute("SELECT last_insert_rowid() AS id").fetchone()
    return int(row["id"])


def resolve_checkpoint_stream(
    db: sqlite3.Connection,
    project: Project,
    stream_id: str | None,
    *,
    include_inactive: bool = False,
) -> str:
    if stream_id is not None:
        return normalize_stream_id(stream_id)
    status_clause = "" if include_inactive else "AND status IN ('active', 'blocked')"
    rows = db.execute(
        f"""
        SELECT DISTINCT stream_id
        FROM checkpoints
        WHERE project_root = ? {status_clause}
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY id DESC
        LIMIT 2
        """,
        (str(project.root),),
    ).fetchall()
    if len(rows) == 1:
        return normalize_stream_id(rows[0]["stream_id"])
    if not rows:
        existing = single_existing_stream_id(db, project)
        if existing:
            return existing
    return DEFAULT_STREAM_ID


def latest_checkpoint(
    db: sqlite3.Connection,
    project: Project,
    stream_id: str | None = None,
) -> sqlite3.Row | None:
    resolved = resolve_checkpoint_stream(db, project, stream_id, include_inactive=True)
    return db.execute(
        """
        SELECT * FROM checkpoints
        WHERE project_root = ? AND stream_id = ?
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END, id DESC
        LIMIT 1
        """,
        (str(project.root), resolved),
    ).fetchone()


def active_checkpoint(
    db: sqlite3.Connection,
    project: Project,
    stream_id: str | None = None,
) -> sqlite3.Row | None:
    resolved = resolve_checkpoint_stream(db, project, stream_id)
    return db.execute(
        """
        SELECT * FROM checkpoints
        WHERE project_root = ? AND stream_id = ? AND status IN ('active', 'blocked')
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(project.root), resolved),
    ).fetchone()


def checkpoint_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def single_existing_stream_id(db: sqlite3.Connection, project: Project) -> str | None:
    rows = db.execute(
        """
        SELECT stream_id, MAX(id) AS last_id
        FROM (
          SELECT stream_id, id FROM checkpoints
          WHERE project_root = ? AND status IN ('active', 'blocked')
            AND (quarantine_reason IS NULL OR quarantine_reason = '')
          UNION ALL
          SELECT stream_id, id FROM events
          WHERE project_root = ?
            AND (quarantine_reason IS NULL OR quarantine_reason = '')
          UNION ALL
          SELECT stream_id, id FROM notes
          WHERE project_root = ? AND status = 'open'
            AND (quarantine_reason IS NULL OR quarantine_reason = '')
        )
        GROUP BY stream_id
        ORDER BY last_id DESC
        LIMIT 2
        """,
        (str(project.root), str(project.root), str(project.root)),
    ).fetchall()
    if len(rows) == 1:
        return normalize_stream_id(rows[0]["stream_id"])
    return None


def checkpoint_file_set(checkpoint: sqlite3.Row | dict[str, Any] | None) -> set[str]:
    if not checkpoint:
        return set()
    raw = str(checkpoint["files_touched"] if isinstance(checkpoint, sqlite3.Row) else checkpoint.get("files_touched") or "")
    files: set[str] = set()
    for part in re.split(r"[\n,]", raw):
        item = part.strip().strip("- ").strip()
        if item:
            files.add(item)
    return files


def checkpoint_subsystems(checkpoint: sqlite3.Row | dict[str, Any] | None) -> set[str]:
    subsystems: set[str] = set()
    for path in checkpoint_file_set(checkpoint):
        pieces = [piece for piece in re.split(r"[\\/]+", path) if piece and piece not in {".", ".."}]
        if pieces:
            subsystems.add("/".join(pieces[:2]))
    return subsystems


def active_peer_checkpoints(
    db: sqlite3.Connection,
    project: Project,
    stream_id: str,
    *,
    limit: int = 8,
) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT * FROM checkpoints
        WHERE project_root = ?
          AND stream_id != ?
          AND status IN ('active', 'blocked')
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (str(project.root), normalize_stream_id(stream_id), limit),
    ).fetchall()


def stream_summary(row: sqlite3.Row) -> dict[str, Any]:
    files = sorted(checkpoint_file_set(row))
    return {
        "stream_id": row["stream_id"],
        "stream_label": row["stream_label"] or "",
        "status": row["status"],
        "objective": normalize_text(row["objective"], 260, redact=False),
        "updated_at": row["updated_at"],
        "files_touched": ", ".join(files[:6]),
    }


def list_streams(db: sqlite3.Connection, project: Project) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT *
        FROM checkpoints
        WHERE project_root = ?
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,
                 updated_at DESC,
                 id DESC
        """,
        (str(project.root),),
    ).fetchall()
    seen: set[str] = set()
    streams: list[dict[str, Any]] = []
    for row in rows:
        stream_id = normalize_stream_id(row["stream_id"])
        if stream_id in seen:
            continue
        seen.add(stream_id)
        streams.append(stream_summary(row))
    return streams


def same_objective_fingerprint(left: str, right: str) -> bool:
    return hash_text(normalize_for_fingerprint(left))[:18] == hash_text(normalize_for_fingerprint(right))[:18]


def peer_conflict_warnings(
    db: sqlite3.Connection,
    project: Project,
    stream_id: str,
    *,
    checkpoint: sqlite3.Row | None = None,
) -> list[str]:
    current = checkpoint or active_checkpoint(db, project, stream_id=stream_id)
    if current is None:
        return []
    current_files = checkpoint_file_set(current)
    current_subsystems = checkpoint_subsystems(current)
    current_objective = str(current["objective"] or "")
    warnings: list[str] = []
    for peer in active_peer_checkpoints(db, project, stream_id, limit=12):
        label = str(peer["stream_label"] or peer["stream_id"])
        peer_files = checkpoint_file_set(peer)
        overlap = sorted(current_files & peer_files)
        if overlap:
            warnings.append(
                f"Peer stream '{label}' also touches {', '.join(overlap[:4])}; coordinate before editing those files."
            )
            continue
        if same_objective_fingerprint(current_objective, str(peer["objective"] or "")):
            warnings.append(
                f"Peer stream '{label}' has a similar active objective; keep this stream's checkpoint authoritative."
            )
            continue
        shared_subsystems = sorted(current_subsystems & checkpoint_subsystems(peer))
        if shared_subsystems:
            warnings.append(
                f"Peer stream '{label}' recently touches the same subsystem ({', '.join(shared_subsystems[:3])}); awareness only."
            )
    return dedupe_keep_order(warnings)[:5]


def update_checkpoint_from_prompt(
    db: sqlite3.Connection,
    project: Project,
    payload: dict[str, Any],
    prompt: str,
    *,
    redact: bool = True,
    stream: StreamScope | None = None,
) -> int | None:
    objective = infer_objective(prompt)
    if not objective:
        return None
    stream = stream or stream_from_payload(payload, project, db=db)
    current = active_checkpoint(db, project, stream_id=stream.id)
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
        stream=stream,
    )


def save_note(
    db: sqlite3.Connection,
    project: Project,
    content: str,
    *,
    surface_condition: str = "",
    status: str = "open",
    redact: bool = True,
    stream: StreamScope | None = None,
    stream_id: str | None = None,
    stream_label: str = "",
    compaction_epoch: int | None = None,
    source_system: str = DEFAULT_SOURCE_SYSTEM,
    source_ref: str = "",
    foreign_project_hint: str = "",
    quarantine_reason: str = "",
    thread_id: str = "",
    imported: bool = False,
    ownership_proven: bool = False,
) -> int:
    stream = stream or coerce_stream(stream_id, stream_label, redact=redact)
    if stream.label:
        remember_stream(db, project, stream)
    clean_content = normalize_text(content, 3000, redact=redact)
    if not clean_content:
        raise ValueError("note content is required")
    now = utc_now()
    if compaction_epoch is None:
        compaction_epoch = current_compaction_epoch(db, project, stream.id)
    if not quarantine_reason:
        detected_hint, detected_reason = quarantine_reason_for_payload(
            project,
            {"thread_id": thread_id},
            summary=clean_content + " " + surface_condition,
            imported=imported,
            ownership_proven=ownership_proven,
        )
        foreign_project_hint = foreign_project_hint or detected_hint
        quarantine_reason = detected_reason
    db.execute(
        """
        INSERT OR IGNORE INTO notes
          (project_root, project_name, stream_id, stream_label, status, content, surface_condition,
           compaction_epoch, source_system, source_ref, foreign_project_hint, quarantine_reason, thread_id,
           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(project.root),
            project.name,
            stream.id,
            stream.label or None,
            normalize_text(status, 80, redact=redact) or "open",
            clean_content,
            normalize_text(surface_condition, 1000, redact=redact),
            int(compaction_epoch or 0),
            normalize_text(source_system, 120, redact=False) or None,
            normalize_text(source_ref, 300, redact=False) or None,
            normalize_text(foreign_project_hint, 500, redact=False) or None,
            normalize_text(quarantine_reason, 300, redact=False) or None,
            normalize_text(thread_id, 300, redact=False) or None,
            now,
            now,
        ),
    )
    db.commit()
    if source_system and source_ref:
        row = db.execute(
            "SELECT id FROM notes WHERE source_system = ? AND source_ref = ? ORDER BY id DESC LIMIT 1",
            (normalize_text(source_system, 120, redact=False), normalize_text(source_ref, 300, redact=False)),
        ).fetchone()
    else:
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
    stream: StreamScope | None = None,
    stream_id: str | None = None,
    stream_label: str = "",
) -> int:
    stream = stream or coerce_stream(stream_id, stream_label, redact=redact)
    if stream.label:
        remember_stream(db, project, stream)
    if field not in CHECKPOINT_TEXT_FIELDS:
        raise ValueError(f"unsupported checkpoint field: {field}")
    checkpoint = active_checkpoint(db, project, stream_id=stream.id)
    if checkpoint is None:
        if not objective:
            raise ValueError("no active checkpoint; pass objective or create a checkpoint first")
        checkpoint_id = save_checkpoint(db, project, objective=objective, redact=redact, stream=stream)
        checkpoint = active_checkpoint(db, project, stream_id=stream.id)
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
    db: sqlite3.Connection,
    project: Project,
    limit: int = DEFAULT_RECENT_EVENT_LIMIT,
    stream_id: str | None = None,
) -> list[sqlite3.Row]:
    resolved = resolve_checkpoint_stream(db, project, stream_id, include_inactive=True)
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ? AND stream_id = ?
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(project.root), resolved, limit),
    ).fetchall()
    return list(reversed(rows))


def recent_notes(
    db: sqlite3.Connection,
    project: Project,
    limit: int = 5,
    stream_id: str | None = None,
) -> list[sqlite3.Row]:
    resolved = resolve_checkpoint_stream(db, project, stream_id, include_inactive=True)
    return db.execute(
        """
        SELECT * FROM notes
        WHERE project_root = ? AND stream_id = ? AND status = 'open'
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(project.root), resolved, limit),
    ).fetchall()


def loop_warnings(
    db: sqlite3.Connection,
    project: Project,
    *,
    stream_id: str | None = None,
    fingerprint_value: str | None = None,
    threshold: int = DEFAULT_LOOP_THRESHOLD,
    include_failure_loop: bool = True,
    include_investigation_loop: bool = True,
    include_tool_output_blindness: bool = True,
) -> list[str]:
    warnings = repeat_warnings(
        db,
        project,
        stream_id=stream_id,
        fingerprint_value=fingerprint_value,
        threshold=threshold,
    )
    if include_failure_loop:
        warnings.extend(failure_loop_warnings(db, project, stream_id=stream_id, threshold=threshold))
    if include_investigation_loop:
        warnings.extend(investigation_loop_warnings(db, project, stream_id=stream_id, threshold=max(threshold + 1, 4)))
    if include_tool_output_blindness:
        warnings.extend(tool_output_blindness_warnings(db, project, stream_id=stream_id))
    return dedupe_keep_order(warnings)[:8]


def repeat_warnings(
    db: sqlite3.Connection,
    project: Project,
    *,
    stream_id: str | None = None,
    fingerprint_value: str | None = None,
    threshold: int = DEFAULT_LOOP_THRESHOLD,
) -> list[str]:
    warnings: list[str] = []
    resolved = resolve_checkpoint_stream(db, project, stream_id, include_inactive=True)
    clauses = ["project_root = ?", "stream_id = ?", "fingerprint IS NOT NULL"]
    params: list[Any] = [str(project.root), resolved]
    if fingerprint_value:
        clauses.append("fingerprint = ?")
        params.append(fingerprint_value)
    rows = db.execute(
        f"""
        SELECT fingerprint, kind, COUNT(*) AS count, MAX(id) AS last_id
        FROM (
          SELECT * FROM events
          WHERE {" AND ".join(clauses)}
            AND (quarantine_reason IS NULL OR quarantine_reason = '')
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
    db: sqlite3.Connection,
    project: Project,
    *,
    stream_id: str | None = None,
    threshold: int = DEFAULT_LOOP_THRESHOLD,
) -> list[str]:
    resolved = resolve_checkpoint_stream(db, project, stream_id, include_inactive=True)
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ? AND stream_id = ? AND event_name = 'PostToolUse'
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY id DESC
        LIMIT 80
        """,
        (str(project.root), resolved),
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
    db: sqlite3.Connection,
    project: Project,
    *,
    stream_id: str | None = None,
    threshold: int = 4,
) -> list[str]:
    resolved = resolve_checkpoint_stream(db, project, stream_id, include_inactive=True)
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ? AND stream_id = ? AND category = 'investigation'
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY id DESC
        LIMIT 24
        """,
        (str(project.root), resolved),
    ).fetchall()
    if len(rows) < threshold:
        return []
    latest = rows[0]
    same = [row for row in rows if row["fingerprint"] == latest["fingerprint"]]
    if len(same) >= threshold:
        return [f"Investigation loop repeated {len(same)} times: {latest['summary'][:220]}"]
    return []


def tool_output_blindness_warnings(
    db: sqlite3.Connection,
    project: Project,
    *,
    stream_id: str | None = None,
) -> list[str]:
    resolved = resolve_checkpoint_stream(db, project, stream_id, include_inactive=True)
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ? AND stream_id = ?
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY id DESC
        LIMIT 6
        """,
        (str(project.root), resolved),
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
    marker = " ..."
    if limit <= len(marker) + 4:
        return text[:limit]
    return text[: limit - len(marker)].rstrip() + marker


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
    stream: StreamScope | None = None,
    compact_epoch: int = 0,
) -> str:
    stream = stream or coerce_stream(DEFAULT_STREAM_ID)
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
        f"<stream>id: {trim_packet_text(stream.id, 48)}; label: {trim_packet_text(stream.label or 'none', 64)}</stream>",
        f"<compact_epoch>{compact_epoch}</compact_epoch>",
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
            "<resume_contract>Use only this stream as active work; peer workstreams are awareness only; verify latest output before completion.</resume_contract>",
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
            f"<stream>id: {trim_packet_text(stream.id, 36)}</stream>",
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
                "<resume_contract>Use only this stream as active work; peers are awareness only.</resume_contract>",
                "</compaction-sentinel>",
            ]
        )
        packet = "\n".join(lines)
        if len(packet) <= max_chars:
            return packet
    minimal_lines = [
        f'<compaction-sentinel version="{VERSION}" schema="packet-v2" reason="{trim_packet_text(reason, 20)}">',
        f"<active_objective>{trim_packet_text(objective, 52)}</active_objective>",
        f"<next_action>{trim_packet_text(next_action, 44)}</next_action>",
        f"<blockers>- {strongest_blocker_line(checkpoint, limit=36)}</blockers>",
        f"<evidence>- {strongest_evidence_line(checkpoint, limit=36)}</evidence>",
        f"<do_not_repeat>- {strongest_do_not_repeat_line(checkpoint, warnings, limit=40)}</do_not_repeat>",
        "<resume_contract>Continue this stream; peers awareness only.</resume_contract>",
        "</compaction-sentinel>",
    ]
    packet = "\n".join(minimal_lines)
    if len(packet) <= max_chars:
        return packet
    return compact_lines(minimal_lines, max_chars=max_chars)


def build_resume_packet(
    db: sqlite3.Connection,
    project: Project,
    *,
    reason: str = "resume",
    max_chars: int = DEFAULT_MAX_PACKET_CHARS,
    loop_threshold: int = DEFAULT_LOOP_THRESHOLD,
    stream: StreamScope | None = None,
    stream_id: str | None = None,
    stream_label: str = "",
) -> str:
    stream = stream or coerce_stream(
        resolve_checkpoint_stream(db, project, stream_id) if stream_id is None else stream_id,
        stream_label,
    )
    checkpoint = active_checkpoint(db, project, stream_id=stream.id)
    latest = latest_checkpoint(db, project, stream_id=stream.id)
    last_checkpoint = latest if not checkpoint and not is_active_checkpoint_row(latest) else None
    events = recent_events(db, project, stream_id=stream.id)
    notes = recent_notes(db, project, stream_id=stream.id)
    warnings = loop_warnings(db, project, stream_id=stream.id, threshold=loop_threshold)
    peers = active_peer_checkpoints(db, project, stream.id)
    peer_conflicts = peer_conflict_warnings(db, project, stream.id, checkpoint=checkpoint)
    compact_epoch = current_compaction_epoch(db, project, stream.id)
    quarantined = quarantine_count(db, project, stream_id=stream.id)
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
        "",
        "<stream>",
        f"id: {escape_packet_text(stream.id)}",
        f"label: {escape_packet_text(stream.label or 'none')}",
        f"compact_epoch: {compact_epoch}",
        f"quarantined_rows: {quarantined}",
        "</stream>",
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

    if peer_conflicts:
        lines.append("")
        lines.append("<peer_conflicts awareness=\"only\">")
        for warning in peer_conflicts:
            lines.append(f"- {escape_packet_text(warning)}")
        lines.append("- Do not replace this stream's next action with a peer stream's next action.")
        lines.append("</peer_conflicts>")

    if peers:
        lines.append("")
        lines.append("<peer_workstreams awareness=\"only\">")
        for peer in peers[:6]:
            summary = stream_summary(peer)
            lines.append(
                "- "
                f"stream={escape_packet_text(summary['stream_id'])}; "
                f"label={escape_packet_text(summary['stream_label'] or 'none')}; "
                f"status={escape_packet_text(summary['status'])}; "
                f"updated_at={escape_packet_text(summary['updated_at'])}; "
                f"objective={escape_packet_text(trim_packet_text(summary['objective'], 220))}; "
                f"files={escape_packet_text(summary['files_touched'] or 'none recorded')}"
            )
        lines.append("- Peer workstreams are awareness only and never the current next action.")
        lines.append("</peer_workstreams>")

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
            "- Use only this stream as active work. Peer workstreams are awareness only.",
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
            stream=stream,
            compact_epoch=compact_epoch,
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


def stop_continue_turn_key(project: Project, payload: dict[str, Any], stream_id: str) -> str:
    return f"{stream_state_prefix(project, stream_id)}stop_continue:turn:{turn_state_id(payload)}"


def stop_continue_checkpoint_key(
    project: Project,
    payload: dict[str, Any],
    checkpoint: sqlite3.Row,
    stream_id: str,
) -> str:
    return f"{stream_state_prefix(project, stream_id)}stop_continue:checkpoint:{turn_state_id(payload)}:{checkpoint['id']}"


def stop_continue_last_signature_key(project: Project, stream_id: str) -> str:
    return f"{stream_state_prefix(project, stream_id)}stop_continue:last_signature"


def stop_continue_next_action_key(project: Project, checkpoint: sqlite3.Row, stream_id: str) -> str:
    return f"{stream_state_prefix(project, stream_id)}stop_continue:next_action:{hash_text(str(checkpoint['next_action'] or ''))[:16]}"


def stop_continue_cooldown_key(project: Project, stream_id: str) -> str:
    return f"{stream_state_prefix(project, stream_id)}stop_continue:last_at"


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
    *,
    stream: StreamScope | None = None,
) -> dict[str, Any] | None:
    stream = stream or stream_from_payload(payload, project, config, db)
    policy = str(config.get("auto_continue") or "off").lower()
    if policy not in {"gentle", "strict"}:
        return None
    if payload.get("stop_hook_active"):
        return None
    checkpoint = active_checkpoint(db, project, stream_id=stream.id)
    if not checkpoint or checkpoint["status"] == "complete":
        return None
    max_per_turn = config_int(config, "stop_continue_max_per_turn", 1, minimum=0)
    max_per_checkpoint = config_int(config, "stop_continue_max_per_checkpoint_per_turn", 1, minimum=0)
    if max_per_turn <= 0:
        return None
    turn_key = stop_continue_turn_key(project, payload, stream.id)
    checkpoint_key = stop_continue_checkpoint_key(project, payload, checkpoint, stream.id)
    if state_count(db, turn_key) >= max_per_turn:
        return None
    if max_per_checkpoint > 0 and state_count(db, checkpoint_key) >= max_per_checkpoint:
        return None
    cooldown_seconds = config_int(config, "stop_continue_cooldown_seconds", 0, minimum=0)
    if within_cooldown(get_state(db, stop_continue_cooldown_key(project, stream.id)), cooldown_seconds):
        return None
    last = extract_last_assistant(payload)
    if looks_complete(last):
        return None
    signature = stop_signature(checkpoint)
    last_signature_key = stop_continue_last_signature_key(project, stream.id)
    if get_state(db, last_signature_key) == signature:
        return None
    if checkpoint["next_action"]:
        next_action_key = stop_continue_next_action_key(project, checkpoint, stream.id)
        if get_state(db, next_action_key) == "used":
            return None
    if loop_warnings(
        db,
        project,
        stream_id=stream.id,
        threshold=config_int(config, "loop_threshold", DEFAULT_LOOP_THRESHOLD, minimum=1),
    ):
        return None
    set_state(db, turn_key, str(state_count(db, turn_key) + 1))
    set_state(db, checkpoint_key, str(state_count(db, checkpoint_key) + 1))
    set_state(db, last_signature_key, signature)
    set_state(db, stop_continue_cooldown_key(project, stream.id), utc_now())
    if checkpoint["next_action"]:
        set_state(db, stop_continue_next_action_key(project, checkpoint, stream.id), "used")
    reason = build_resume_packet(
        db,
        project,
        reason="stop-continuation",
        max_chars=min(config_int(config, "max_packet_chars", DEFAULT_MAX_PACKET_CHARS, minimum=500), 6500),
        loop_threshold=config_int(config, "loop_threshold", DEFAULT_LOOP_THRESHOLD, minimum=1),
        stream=stream,
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
    stream_id: str,
    threshold: int,
    mode: str,
) -> list[str]:
    if mode == "light":
        return []
    return repeat_warnings(db, project, stream_id=stream_id, fingerprint_value=row["fingerprint"], threshold=threshold)


def post_tool_warnings(
    db: sqlite3.Connection,
    project: Project,
    row: sqlite3.Row,
    *,
    stream_id: str,
    command: str,
    threshold: int,
    mode: str,
) -> list[str]:
    if mode == "light":
        return []
    warnings = repeat_warnings(db, project, stream_id=stream_id, fingerprint_value=row["fingerprint"], threshold=threshold)
    category = str(row["category"] or "")
    if category == "tool_failure" or is_test_or_build_command(command):
        warnings.extend(failure_loop_warnings(db, project, stream_id=stream_id, threshold=threshold))
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
    stream = stream_from_payload(payload, project, config, db)
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

    if event_name in {"PreCompact", "PostCompact"}:
        if event_name == "PreCompact":
            epoch = advance_compaction_epoch(db, project, stream.id)
        else:
            epoch = current_compaction_epoch(db, project, stream.id)
            if epoch <= 0:
                epoch = advance_compaction_epoch(db, project, stream.id)
        checkpoint = active_checkpoint(db, project, stream_id=stream.id)
        trigger = payload.get("trigger") or payload.get("source") or payload.get("reason") or "unknown"
        record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind="compact",
            summary=f"{event_name}: {trigger}",
            details={
                "trigger": trigger,
                "capture_only": not compact_context_enabled(config),
                "active_checkpoint": checkpoint_to_dict(checkpoint),
            },
            redact=redact,
            max_events=max_events,
            retention_days=retention_days,
            retention_check_interval_seconds=retention_interval,
            prune_check_interval_events=prune_interval,
            stream=stream,
            compaction_epoch=epoch,
        )
        if compact_context_enabled(config):
            packet = build_resume_packet(
                db,
                project,
                reason=event_name.lower(),
                max_chars=max_chars,
                loop_threshold=loop_threshold,
                stream=stream,
            )
            return hook_output(event_name, packet)
        return {}

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
            stream=stream,
        )
        update_checkpoint_from_prompt(db, project, payload, prompt, redact=redact, stream=stream)
        packet = build_resume_packet(
            db,
            project,
            reason="user-prompt",
            max_chars=max_chars,
            loop_threshold=loop_threshold,
            stream=stream,
        )
        warnings = loop_warnings(
            db,
            project,
            stream_id=stream.id,
            fingerprint_value=row["fingerprint"],
            threshold=loop_threshold,
        )
        message = (
            "Compaction Sentinel detected repeated context; inspect the latest artifact before repeating."
            if warnings
            else None
        )
        return hook_output(event_name, packet, message)

    if event_name == "SessionStart":
        source = str(payload.get("source") or "unknown")
        record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind="session",
            summary=f"Session start: {source}",
            details={"source": source},
            redact=redact,
            max_events=max_events,
            retention_days=retention_days,
            retention_check_interval_seconds=retention_interval,
            prune_check_interval_events=prune_interval,
            stream=stream,
        )
        is_compact_start = source.strip().lower() == "compact"
        if is_compact_start and not compact_context_enabled(config):
            return {}
        packet = build_resume_packet(
            db,
            project,
            reason="compact-session-start" if is_compact_start else "session-start",
            max_chars=max_chars,
            loop_threshold=loop_threshold,
            stream=stream,
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
            stream=stream,
        )
        warnings = pre_tool_warnings(db, project, row, stream_id=stream.id, threshold=loop_threshold, mode=mode)
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
        checkpoint = active_checkpoint(db, project, stream_id=stream.id)
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
            stream=stream,
        )
        warnings = repeat_warnings(
            db,
            project,
            stream_id=stream.id,
            fingerprint_value=row["fingerprint"],
            threshold=loop_threshold,
        )
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
            stream=stream,
        )
        warnings = post_tool_warnings(
            db,
            project,
            row,
            stream_id=stream.id,
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
            stream=stream,
        )
        warnings = loop_warnings(db, project, stream_id=stream.id, threshold=loop_threshold)
        continuation = maybe_stop_continue(db, project, payload, config, stream=stream)
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
        stream=stream,
    )
    return {}


def search_events(
    db: sqlite3.Connection,
    project: Project,
    query: str,
    *,
    limit: int = 8,
    stream_id: str | None = None,
) -> list[sqlite3.Row]:
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_./:-]{2,}", query)]
    resolved = resolve_checkpoint_stream(db, project, stream_id, include_inactive=True)
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ? AND stream_id = ?
          AND (quarantine_reason IS NULL OR quarantine_reason = '')
        ORDER BY id DESC
        LIMIT 200
        """,
        (str(project.root), resolved),
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


def scrub_project(
    db: sqlite3.Connection,
    project: Project,
    *,
    stream_id: str | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    resolved = normalize_stream_id(stream_id) if stream_id is not None else None
    for table in ("events", "notes", "checkpoints", "memory_candidates"):
        if resolved is None:
            where = "project_root = ?"
            params: tuple[Any, ...] = (str(project.root),)
        else:
            where = "project_root = ? AND stream_id = ?"
            params = (str(project.root), resolved)
        row = db.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {where}", params).fetchone()
        counts[table] = int(row["count"] if row else 0)
        db.execute(f"DELETE FROM {table} WHERE {where}", params)
    prefix = stream_state_prefix(project, resolved) if resolved is not None else project_state_prefix(project)
    state_keys = {str(row["key"]) for row in db.execute("SELECT key FROM state WHERE key LIKE ?", (prefix + "%",)).fetchall()}
    if resolved is None:
        # Remove legacy v0.3 keys that stored the raw project path.
        raw_root = str(project.root)
        state_keys.update(
            str(row["key"])
            for row in db.execute("SELECT key FROM state").fetchall()
            if raw_root in str(row["key"])
        )
    else:
        state_keys.update(
            str(row["key"])
            for row in db.execute("SELECT key FROM state WHERE key LIKE ?", (project_state_prefix(project) + "session_stream:%",)).fetchall()
            if get_state(db, str(row["key"])) == resolved
        )
    counts["state"] = len(state_keys)
    for key in state_keys:
        db.execute("DELETE FROM state WHERE key = ?", (key,))
    db.commit()
    return counts


def scrub_all(db: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("events", "notes", "checkpoints", "memory_candidates", "state"):
        row = db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        counts[table] = int(row["count"] if row else 0)
        db.execute(f"DELETE FROM {table}")
    db.commit()
    return counts


def save_memory_candidate(
    db: sqlite3.Connection,
    project: Project,
    *,
    content: str,
    title: str = "",
    tags: str = "",
    importance: str = "",
    stream: StreamScope | None = None,
    stream_id: str | None = None,
    stream_label: str = "",
    source_system: str = DEFAULT_SOURCE_SYSTEM,
    source_ref: str = "",
    foreign_project_hint: str = "",
    quarantine_reason: str = "",
    thread_id: str = "",
    imported: bool = False,
    ownership_proven: bool = False,
    redact: bool = True,
) -> int:
    stream = stream or coerce_stream(stream_id, stream_label, redact=redact)
    clean_content = normalize_text(content, 6000, redact=redact)
    if not clean_content:
        raise ValueError("memory candidate content is required")
    if not quarantine_reason:
        detected_hint, detected_reason = quarantine_reason_for_payload(
            project,
            {"thread_id": thread_id},
            summary=clean_content + " " + title + " " + tags,
            imported=imported,
            ownership_proven=ownership_proven,
        )
        foreign_project_hint = foreign_project_hint or detected_hint
        quarantine_reason = detected_reason
    now = utc_now()
    db.execute(
        """
        INSERT OR IGNORE INTO memory_candidates
          (project_root, project_name, stream_id, stream_label, content, title, tags, importance,
           source_system, source_ref, foreign_project_hint, quarantine_reason, thread_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(project.root),
            project.name,
            stream.id,
            stream.label or None,
            clean_content,
            normalize_text(title, 400, redact=redact),
            normalize_text(tags, 400, redact=redact),
            normalize_text(importance, 80, redact=redact),
            normalize_text(source_system, 120, redact=False) or None,
            normalize_text(source_ref, 300, redact=False) or None,
            normalize_text(foreign_project_hint, 500, redact=False) or None,
            normalize_text(quarantine_reason, 300, redact=False) or None,
            normalize_text(thread_id, 300, redact=False) or None,
            now,
            now,
        ),
    )
    db.commit()
    if source_system and source_ref:
        row = db.execute(
            "SELECT id FROM memory_candidates WHERE source_system = ? AND source_ref = ? ORDER BY id DESC LIMIT 1",
            (normalize_text(source_system, 120, redact=False), normalize_text(source_ref, 300, redact=False)),
        ).fetchone()
    else:
        row = db.execute("SELECT last_insert_rowid() AS id").fetchone()
    return int(row["id"])


def list_memory_candidates(
    db: sqlite3.Connection,
    project: Project | None = None,
    *,
    include_quarantined: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if project is not None:
        clauses.append("project_root = ?")
        params.append(str(project.root))
    if not include_quarantined:
        clauses.append("(quarantine_reason IS NULL OR quarantine_reason = '')")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return [
        dict(row)
        for row in db.execute(
            f"SELECT * FROM memory_candidates {where} ORDER BY id DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
    ]


def export_project(
    db: sqlite3.Connection,
    project: Project,
    *,
    stream_id: str | None = None,
) -> dict[str, Any]:
    resolved = normalize_stream_id(stream_id) if stream_id is not None else None
    if resolved is None:
        where = "project_root = ?"
        params: tuple[Any, ...] = (str(project.root),)
    else:
        where = "project_root = ? AND stream_id = ?"
        params = (str(project.root), resolved)
    checkpoints = [
        dict(row)
        for row in db.execute(
            f"SELECT * FROM checkpoints WHERE {where} ORDER BY id DESC",
            params,
        ).fetchall()
    ]
    notes = [
        dict(row)
        for row in db.execute(
            f"SELECT * FROM notes WHERE {where} ORDER BY id DESC",
            params,
        ).fetchall()
    ]
    events = []
    for row in db.execute(
        f"SELECT * FROM events WHERE {where} ORDER BY id DESC",
        params,
    ).fetchall():
        item = dict(row)
        try:
            item["details"] = json.loads(str(item.pop("details_json") or "{}"))
        except Exception:
            item["details"] = {}
        events.append(item)
    memory_candidates = [
        dict(row)
        for row in db.execute(
            f"SELECT * FROM memory_candidates WHERE {where} ORDER BY id DESC",
            params,
        ).fetchall()
    ]
    return {
        "version": VERSION,
        "exported_at": utc_now(),
        "project": project.name,
        "project_root": str(project.root),
        "stream_id": resolved,
        "checkpoints": checkpoints,
        "notes": notes,
        "events": events,
        "memory_candidates": memory_candidates,
    }


def compact_status(db: sqlite3.Connection, project: Project, stream: StreamScope) -> dict[str, Any]:
    return {
        "project": project.name,
        "project_root": str(project.root),
        "current_stream": {"stream_id": stream.id, "stream_label": stream.label},
        "compaction_epoch": current_compaction_epoch(db, project, stream.id),
        "compact_context_smoke_passed": False,
        "quarantine_count": quarantine_count(db, project, stream_id=stream.id),
    }


def compact_audit(
    db: sqlite3.Connection,
    project: Project,
    stream: StreamScope,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE project_root = ?
          AND stream_id = ?
          AND event_name IN ('PreCompact', 'PostCompact')
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(project.root), stream.id, int(limit)),
    ).fetchall()
    return {
        **compact_status(db, project, stream),
        "events": [dict(row) for row in rows],
    }


def codex_context_db_path(codex_home: Path) -> Path:
    return codex_home / "codex-context" / "context.db"


def sqlite_table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def row_value(row: sqlite3.Row, key: str, default: Any = "") -> Any:
    return row[key] if key in row.keys() else default


def source_ref_for_row(table: str, row: sqlite3.Row) -> str:
    stable = row_value(row, "content_hash") or hash_text(
        json.dumps({key: row[key] for key in row.keys()}, ensure_ascii=False, sort_keys=True, default=str)
    )[:24]
    return f"{table}:{row_value(row, 'id')}:{stable}"


def project_from_codex_context_path(value: Any) -> Project:
    raw = str(value or "").strip()
    root = find_project_root(Path(raw).expanduser()) if raw else Path.cwd()
    return Project(root=root, name=root.name or "workspace")


def project_ownership_proven(project: Project, source_project_path: Any, content: str = "") -> bool:
    if not source_project_path:
        return False
    try:
        source_root = find_project_root(Path(str(source_project_path)).expanduser())
        same_root = source_root.resolve(strict=False) == project.root.resolve(strict=False)
    except Exception:
        same_root = False
    if not same_root:
        return False
    hint, reason = quarantine_reason_for_payload(
        project,
        {"project_path": str(source_project_path)},
        summary=content,
        imported=True,
        ownership_proven=True,
    )
    return not reason and not hint


def inspect_codex_context(codex_home: Path) -> dict[str, Any]:
    path = codex_context_db_path(codex_home)
    result: dict[str, Any] = {
        "codex_context_db": str(path),
        "exists": path.exists(),
        "readable": False,
        "counts": {"records": 0, "notes": 0, "checkpoints": 0},
        "planned_actions": [],
    }
    if not path.exists():
        result["planned_actions"].append("no codex-context database found")
        return result
    try:
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        result["readable"] = True
        for table in ("records", "notes", "checkpoints"):
            if sqlite_table_exists(source, table):
                row = source.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                result["counts"][table] = int(row["count"] if row else 0)
        result["planned_actions"].extend(
            [
                "import records as memory candidates",
                "import notes as Sentinel notes",
                "import checkpoints as checkpoints only when objective ownership is proven; otherwise notes",
                "quarantine imported rows unless project ownership is proven",
            ]
        )
        source.close()
    except Exception as exc:
        result["error"] = str(exc)
    return result


def backup_codex_context_state(codex_home: Path) -> dict[str, Any]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = codex_home / "backups" / "compaction-sentinel" / f"codex-context-migration-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    for label, path in {
        "codex_context_db": codex_context_db_path(codex_home),
        "hooks_json": codex_home / "hooks.json",
        "config_toml": codex_home / "config.toml",
    }.items():
        if path.exists():
            target = backup_dir / path.name
            shutil.copy2(path, target)
            copied[label] = str(target)
    return {"backup_dir": str(backup_dir), "files": copied}


def write_migration_rollback(codex_home: Path, manifest: dict[str, Any]) -> Path:
    backup_dir = Path(str(manifest.get("backup", {}).get("backup_dir") or codex_home / "backups" / "compaction-sentinel"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / "rollback.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def import_codex_context(
    codex_home: Path,
    *,
    sentinel_db: sqlite3.Connection,
    redact: bool = True,
) -> dict[str, Any]:
    source_path = codex_context_db_path(codex_home)
    result: dict[str, Any] = {
        "source": str(source_path),
        "imported": {"records": 0, "notes": 0, "checkpoints": 0, "checkpoint_notes": 0},
        "skipped": {"records": 0, "notes": 0, "checkpoints": 0},
    }
    if not source_path.exists():
        result["error"] = "codex-context database not found"
        return result
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        if sqlite_table_exists(source, "records"):
            for row in source.execute("SELECT * FROM records ORDER BY id").fetchall():
                project = project_from_codex_context_path(row_value(row, "project_path"))
                content = "\n".join(
                    part
                    for part in (
                        str(row_value(row, "title") or ""),
                        str(row_value(row, "content") or ""),
                        str(row_value(row, "source_path") or ""),
                    )
                    if part
                )
                ownership = project_ownership_proven(project, row_value(row, "project_path"), content)
                save_memory_candidate(
                    sentinel_db,
                    project,
                    title=str(row_value(row, "title") or ""),
                    content=content,
                    tags=str(row_value(row, "tags") or ""),
                    importance=str(row_value(row, "importance") or ""),
                    source_system="codex-context",
                    source_ref=source_ref_for_row("records", row),
                    imported=True,
                    ownership_proven=ownership,
                    redact=redact,
                )
                result["imported"]["records"] += 1
        if sqlite_table_exists(source, "notes"):
            for row in source.execute("SELECT * FROM notes ORDER BY id").fetchall():
                project = project_from_codex_context_path(row_value(row, "project_path"))
                content = str(row_value(row, "content") or "")
                ownership = project_ownership_proven(project, row_value(row, "project_path"), content)
                save_note(
                    sentinel_db,
                    project,
                    content,
                    surface_condition=str(row_value(row, "surface_condition") or ""),
                    status=str(row_value(row, "status") or "open"),
                    source_system="codex-context",
                    source_ref=source_ref_for_row("notes", row),
                    imported=True,
                    ownership_proven=ownership,
                    redact=redact,
                )
                result["imported"]["notes"] += 1
        if sqlite_table_exists(source, "checkpoints"):
            for row in source.execute("SELECT * FROM checkpoints ORDER BY id").fetchall():
                project = project_from_codex_context_path(row_value(row, "project_path") or row_value(row, "cwd"))
                summary = str(row_value(row, "summary") or "")
                prompt = str(row_value(row, "prompt_excerpt") or "")
                response = str(row_value(row, "response_excerpt") or "")
                content = "\n".join(
                    part
                    for part in (
                        summary,
                        prompt,
                        response,
                        str(row_value(row, "cwd") or ""),
                        str(row_value(row, "transcript_path") or ""),
                    )
                    if part
                )
                ownership = project_ownership_proven(
                    project,
                    row_value(row, "project_path") or row_value(row, "cwd"),
                    content,
                )
                objective = infer_objective(prompt) or infer_objective(summary)
                source_ref = source_ref_for_row("checkpoints", row)
                if ownership and objective:
                    save_checkpoint(
                        sentinel_db,
                        project,
                        objective=objective,
                        current_step=normalize_text(summary or response, 1400, redact=redact),
                        next_action="Continue from the imported codex-context checkpoint after verifying current files.",
                        evidence=normalize_text(response, 1800, redact=redact),
                        source="codex-context",
                        source_system="codex-context",
                        source_ref=source_ref,
                        session_id=str(row_value(row, "session_id") or ""),
                        thread_id=str(row_value(row, "session_id") or ""),
                        imported=True,
                        ownership_proven=True,
                        redact=redact,
                    )
                    result["imported"]["checkpoints"] += 1
                else:
                    save_note(
                        sentinel_db,
                        project,
                        content or "Imported codex-context checkpoint with no portable summary.",
                        surface_condition="imported codex-context checkpoint; not active objective",
                        source_system="codex-context",
                        source_ref=source_ref,
                        imported=True,
                        ownership_proven=ownership,
                        redact=redact,
                    )
                    result["imported"]["checkpoint_notes"] += 1
    finally:
        source.close()
    set_state(
        sentinel_db,
        "migration:codex-context:last_import",
        json.dumps(result, sort_keys=True),
    )
    return result
