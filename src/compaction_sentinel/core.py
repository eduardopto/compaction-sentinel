"""Core storage, hook handling, and resume-packet logic for Compaction Sentinel."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


VERSION = "0.2.0"
APP_NAME = "Compaction Sentinel"
DEFAULT_MAX_PACKET_CHARS = 9000
DEFAULT_LOOP_THRESHOLD = 3
DEFAULT_RECENT_EVENT_LIMIT = 18
DEFAULT_MAX_EVENTS_PER_PROJECT = 1200
SENSITIVE_VALUE = "[redacted]"


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


def load_runtime_config(codex_home: Path | None = None) -> dict[str, Any]:
    path = config_path(codex_home)
    default = {
        "version": 1,
        "max_packet_chars": DEFAULT_MAX_PACKET_CHARS,
        "loop_threshold": DEFAULT_LOOP_THRESHOLD,
        "auto_continue": "off",
        "stop_continue_max_per_turn": 1,
        "max_events_per_project": DEFAULT_MAX_EVENTS_PER_PROJECT,
        "redact": True,
    }
    if not path.exists():
        return default
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    if isinstance(loaded, dict):
        merged = {**default, **loaded}
        return merged
    return default


def write_runtime_config(config: dict[str, Any], codex_home: Path | None = None) -> None:
    root = install_root(codex_home)
    root.mkdir(parents=True, exist_ok=True)
    path = config_path(codex_home)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=2.0)
    db.row_factory = sqlite3.Row
    init_db(db)
    return db


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
          summary TEXT NOT NULL,
          details_json TEXT NOT NULL,
          fingerprint TEXT,
          outcome TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_project_id
          ON events(project_root, id DESC);
        CREATE INDEX IF NOT EXISTS idx_events_fingerprint
          ON events(project_root, fingerprint, id DESC);

        CREATE TABLE IF NOT EXISTS checkpoints (
          id INTEGER PRIMARY KEY,
          project_root TEXT NOT NULL,
          project_name TEXT NOT NULL,
          session_id TEXT,
          turn_id TEXT,
          status TEXT NOT NULL DEFAULT 'active',
          objective TEXT NOT NULL,
          current_step TEXT,
          next_action TEXT,
          blockers TEXT,
          evidence TEXT,
          source TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_checkpoints_project_id
          ON checkpoints(project_root, id DESC);

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

        CREATE TABLE IF NOT EXISTS state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    existing_columns = {
        str(row["name"])
        for row in db.execute("PRAGMA table_info(checkpoints)").fetchall()
    }
    if "completed_at" not in existing_columns:
        db.execute("ALTER TABLE checkpoints ADD COLUMN completed_at TEXT")
    db.execute("PRAGMA busy_timeout=2000;")
    db.commit()


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


SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{24,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password|authorization|bearer)\b"
        r"\s*[:=]\s*['\"]?[^'\"\s,;]{6,}"
    ),
]


def redact_text(text: str) -> str:
    if not text:
        return ""
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: _redact_match(m.group(0)), redacted)
    return redacted


def _redact_match(value: str) -> str:
    if ":" in value or "=" in value:
        key = re.split(r"[:=]", value, maxsplit=1)[0].strip()
        return f"{key}={SENSITIVE_VALUE}"
    if value.startswith("sk-"):
        return "sk-" + SENSITIVE_VALUE
    return SENSITIVE_VALUE


def normalize_text(value: Any, limit: int = 3000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(value)
    text = redact_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 20].rstrip() + " ...[truncated]"
    return text


def sanitize_json(value: Any, *, text_limit: int = 3000) -> Any:
    if isinstance(value, str):
        return normalize_text(value, text_limit)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = normalize_text(str(key), 200)
            if re.search(r"(?i)(api[_-]?key|token|secret|password|authorization)", safe_key):
                out[safe_key] = SENSITIVE_VALUE
            else:
                out[safe_key] = sanitize_json(item, text_limit=text_limit)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item, text_limit=text_limit) for item in value[:50]]
    return normalize_text(value, text_limit)


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def fingerprint(kind: str, summary: str) -> str:
    normalized = summary.lower()
    normalized = re.sub(r"/Users/[^ ]+", "/PATH", normalized)
    normalized = re.sub(r"\b[0-9a-f]{7,40}\b", "HASH", normalized)
    normalized = re.sub(r"\d+", "N", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hash_text(f"{kind}:{normalized}")[:24]


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


def extract_prompt(payload: dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "message", "input", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value, 6000)
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


def extract_tool_input(payload: dict[str, Any]) -> str:
    value = payload.get("tool_input")
    if value is None:
        value = payload.get("input")
    if isinstance(value, dict):
        command = value.get("command") or value.get("cmd")
        if isinstance(command, str) and command.strip():
            return normalize_text(command, 3000)
    return normalize_text(value, 3000)


def extract_tool_response(payload: dict[str, Any]) -> tuple[str, str | None]:
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
        return normalize_text(combined or value, 3000), outcome
    return normalize_text(value, 3000), outcome


def extract_last_assistant(payload: dict[str, Any]) -> str:
    for key in ("last_assistant_message", "assistant_message", "message", "output"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value, 4000)
    return ""


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
) -> sqlite3.Row:
    clean_summary = normalize_text(summary, 4000)
    clean_details = sanitize_json(details or {})
    now = utc_now()
    fp = fingerprint(kind, clean_summary) if clean_summary else None
    db.execute(
        """
        INSERT INTO events
          (project_root, project_name, session_id, turn_id, event_name, kind,
           summary, details_json, fingerprint, outcome, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(project.root),
            project.name,
            str(payload.get("session_id") or ""),
            str(payload.get("turn_id") or ""),
            event_name,
            kind,
            clean_summary,
            json.dumps(clean_details, ensure_ascii=False, sort_keys=True),
            fp,
            outcome,
            now,
        ),
    )
    db.commit()
    prune_events(db, project)
    row = db.execute("SELECT * FROM events WHERE id = last_insert_rowid()").fetchone()
    assert row is not None
    return row


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


def infer_objective(prompt: str) -> str | None:
    if not prompt:
        return None
    patterns = [
        r"(?is)\bset\s+goal\s*:\s*(.+)",
        r"(?is)\bgoal\s*:\s*(.+)",
        r"(?is)\bdo\s+not\s+stop\s+until\s+(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, prompt)
        if match:
            objective = match.group(1).strip()
            objective = re.split(r"\n\s*\n", objective, maxsplit=1)[0].strip()
            objective = re.sub(r"\s+", " ", objective)
            if len(objective) > 900:
                objective = objective[:880].rstrip() + " ..."
            if objective:
                return objective
    return None


def save_checkpoint(
    db: sqlite3.Connection,
    project: Project,
    *,
    objective: str,
    status: str = "active",
    current_step: str = "",
    next_action: str = "",
    blockers: str = "",
    evidence: str = "",
    source: str = "manual",
    session_id: str = "",
    turn_id: str = "",
) -> int:
    clean_objective = normalize_text(objective, 1400)
    if not clean_objective:
        raise ValueError("checkpoint objective is required")
    now = utc_now()
    clean_status = normalize_text(status, 80) or "active"
    close_status = "complete" if clean_status == "complete" else "superseded"
    db.execute(
        """
        UPDATE checkpoints
        SET status = ?, completed_at = COALESCE(completed_at, ?), updated_at = ?
        WHERE project_root = ? AND status IN ('active', 'blocked')
        """,
        (close_status, now, now, str(project.root)),
    )
    db.execute(
        """
        INSERT INTO checkpoints
          (project_root, project_name, session_id, turn_id, status, objective,
           current_step, next_action, blockers, evidence, source, created_at, updated_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(project.root),
            project.name,
            session_id,
            turn_id,
            clean_status,
            clean_objective,
            normalize_text(current_step, 1400),
            normalize_text(next_action, 1400),
            normalize_text(blockers, 1400),
            normalize_text(evidence, 2400),
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


def update_checkpoint_from_prompt(
    db: sqlite3.Connection, project: Project, payload: dict[str, Any], prompt: str
) -> int | None:
    objective = infer_objective(prompt)
    if not objective:
        return None
    current = active_checkpoint(db, project)
    if current and normalize_text(current["objective"], 900) == normalize_text(objective, 900):
        return int(current["id"])
    return save_checkpoint(
        db,
        project,
        objective=objective,
        status="active",
        current_step="User set or refreshed the active goal.",
        next_action="Continue from the active task state; do not restart from stale context.",
        source="UserPromptSubmit",
        session_id=str(payload.get("session_id") or ""),
        turn_id=str(payload.get("turn_id") or ""),
    )


def save_note(
    db: sqlite3.Connection,
    project: Project,
    content: str,
    *,
    surface_condition: str = "",
    status: str = "open",
) -> int:
    clean_content = normalize_text(content, 3000)
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
            status,
            clean_content,
            normalize_text(surface_condition, 1000),
            now,
            now,
        ),
    )
    db.commit()
    row = db.execute("SELECT last_insert_rowid() AS id").fetchone()
    return int(row["id"])


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
) -> list[str]:
    clauses = ["project_root = ?", "fingerprint IS NOT NULL"]
    params: list[Any] = [str(project.root)]
    if fingerprint_value:
        clauses.append("fingerprint = ?")
        params.append(fingerprint_value)
    rows = db.execute(
        f"""
        SELECT fingerprint, kind, summary, COUNT(*) AS count, MAX(id) AS last_id
        FROM (
          SELECT * FROM events
          WHERE {" AND ".join(clauses)}
          ORDER BY id DESC
          LIMIT 60
        )
        GROUP BY fingerprint, kind, summary
        HAVING count >= ?
        ORDER BY count DESC, last_id DESC
        LIMIT 5
        """,
        (*params, threshold),
    ).fetchall()
    warnings: list[str] = []
    for row in rows:
        warnings.append(
            f"{row['kind']} repeated {row['count']} times: {row['summary'][:220]}"
        )
    return warnings


def compact_lines(lines: Iterable[str], max_chars: int) -> str:
    out: list[str] = []
    used = 0
    for line in lines:
        line = line.rstrip()
        cost = len(line) + 1
        if used + cost > max_chars:
            out.append("...[packet truncated to stay compact]")
            break
        out.append(line)
        used += cost
    return "\n".join(out).strip()


def build_resume_packet(
    db: sqlite3.Connection,
    project: Project,
    *,
    reason: str = "resume",
    max_chars: int = DEFAULT_MAX_PACKET_CHARS,
    loop_threshold: int = DEFAULT_LOOP_THRESHOLD,
) -> str:
    checkpoint = active_checkpoint(db, project) or latest_checkpoint(db, project)
    events = recent_events(db, project)
    notes = recent_notes(db, project)
    warnings = loop_warnings(db, project, threshold=loop_threshold)

    lines: list[str] = [
        f'<compaction-sentinel version="{VERSION}" reason="{reason}">',
        "Purpose: preserve active work across Codex Desktop compaction/resume boundaries.",
        f"Project: {project.name}",
        f"Project root: {project.root}",
        f"Host OS: {platform.system()} {platform.release()}",
    ]
    if checkpoint:
        lines.extend(
            [
                "",
                "Active checkpoint:",
                f"- status: {checkpoint['status']}",
                f"- objective: {checkpoint['objective']}",
                f"- current_step: {checkpoint['current_step'] or 'unknown'}",
                f"- next_action: {checkpoint['next_action'] or 'verify current files, then continue'}",
            ]
        )
        if checkpoint["blockers"]:
            lines.append(f"- blockers: {checkpoint['blockers']}")
        if checkpoint["evidence"]:
            lines.append(f"- evidence: {checkpoint['evidence']}")
    if notes:
        lines.append("")
        lines.append("Open continuity notes:")
        for note in notes:
            suffix = f" when {note['surface_condition']}" if note["surface_condition"] else ""
            lines.append(f"- {note['content']}{suffix}")
    if warnings:
        lines.append("")
        lines.append("Loop guard:")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("- Before repeating, inspect the concrete artifact/log/result and choose one new hypothesis.")
    if events:
        lines.append("")
        lines.append("Recent event trail:")
        for event in events:
            time = str(event["created_at"]).replace("+00:00", "Z")
            outcome = f" outcome={event['outcome']}" if event["outcome"] else ""
            lines.append(f"- {time} {event['event_name']}/{event['kind']}{outcome}: {event['summary']}")
    lines.extend(
        [
            "",
            "Resume contract:",
            "- Continue the live objective from this packet; do not restart from only the last user message.",
            "- Treat checkpoints, notes, and current files as the authority when they conflict with stale summaries.",
            "- Preserve user acceptance criteria in natural language.",
            "- Update Compaction Sentinel with a checkpoint when the current step, blocker, or next action changes.",
            "</compaction-sentinel>",
        ]
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
    last = extract_last_assistant(payload).lower()
    completion_markers = (
        "marked the goal complete",
        "goal complete",
        "done",
        "completed",
        "all tests passed",
    )
    if policy == "gentle" and any(marker in last for marker in completion_markers):
        return None
    reason = build_resume_packet(
        db,
        project,
        reason="stop-continuation",
        max_chars=min(int(config.get("max_packet_chars") or DEFAULT_MAX_PACKET_CHARS), 6500),
        loop_threshold=int(config.get("loop_threshold") or DEFAULT_LOOP_THRESHOLD),
    )
    reason += "\n\nContinue this active objective. First state the next concrete action, then perform it."
    return {"decision": "block", "reason": reason}


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
    project = project_from_payload(payload)
    event_name = event_name or str(payload.get("hook_event_name") or "")
    loop_threshold = int(config.get("loop_threshold") or DEFAULT_LOOP_THRESHOLD)
    max_chars = int(config.get("max_packet_chars") or DEFAULT_MAX_PACKET_CHARS)

    if event_name == "UserPromptSubmit":
        prompt = extract_prompt(payload)
        row = record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind="prompt",
            summary=prompt or "User prompt submitted.",
            details={"prompt": prompt},
        )
        update_checkpoint_from_prompt(db, project, payload, prompt)
        packet = build_resume_packet(
            db,
            project,
            reason="user-prompt",
            max_chars=max_chars,
            loop_threshold=loop_threshold,
        )
        warnings = loop_warnings(db, project, fingerprint_value=row["fingerprint"], threshold=loop_threshold)
        message = "Compaction Sentinel detected repeated context; inspect the latest artifact before repeating." if warnings else None
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
        tool_input = extract_tool_input(payload)
        row = record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind=f"tool:{tool_name}",
            summary=tool_input or f"{tool_name} invoked",
            details={"tool_name": tool_name, "tool_input": tool_input},
        )
        warnings = loop_warnings(db, project, fingerprint_value=row["fingerprint"], threshold=loop_threshold)
        if warnings:
            return hook_output(
                event_name,
                "Compaction Sentinel loop warning:\n"
                + "\n".join(f"- {warning}" for warning in warnings)
                + "\nBefore repeating this tool call, inspect the newest concrete evidence and change the hypothesis.",
                "Repeated tool pattern detected.",
            )
        return {}

    if event_name == "PostToolUse":
        tool_name = extract_tool_name(payload)
        response, outcome = extract_tool_response(payload)
        tool_input = extract_tool_input(payload)
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
        )
        warnings = loop_warnings(db, project, fingerprint_value=row["fingerprint"], threshold=loop_threshold)
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
        last = extract_last_assistant(payload)
        record_event(
            db,
            project,
            payload,
            event_name=event_name,
            kind="stop",
            summary=last or "Turn stopped.",
            details={"last_assistant_message": last},
        )
        continuation = maybe_stop_continue(db, project, payload, config)
        if continuation:
            return continuation
        return {}

    record_event(
        db,
        project,
        payload,
        event_name=event_name or "Unknown",
        kind="unknown",
        summary=f"Unhandled hook event: {event_name}",
        details=payload,
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
        haystack = f"{row['summary']} {row['details_json']} {row['event_name']} {row['kind']}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
    return [row for _, row in scored[:limit]]


def project_from_cli(cwd: str | None = None) -> Project:
    root = find_project_root(Path(cwd or os.getcwd()))
    return Project(root=root, name=root.name or "workspace")
