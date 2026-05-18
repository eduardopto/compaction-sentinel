# Changelog

## 0.3.0

- Adds packet v2 with authority, acceptance criteria, current state, next action, blockers, evidence, do-not-repeat items, and resume contract.
- Adds `PermissionRequest` hook recording without automatic approval or denial.
- Adds regression warnings for repeated commands, repeated failures, investigation loops, stale restarts, revert/oscillation risk, repeated permissions, fake completion, and tool-output blindness.
- Enforces Stop auto-continue caps and cooldown state; auto-continue remains off by default.
- Expands checkpoints with acceptance criteria, files touched, commands run, tests passed/failed, decisions, assumptions, do-not-repeat, last verified time, and confidence.
- Adds CLI/MCP operations for evidence and avoid-list updates.
- Adds `cs scrub`, `cs export`, `cs retention`, `cs config`, `cs backup`, `cs doctor --fix`, `cs doctor --explain`, and `cs uninstall --purge`.
- Adds package assets so pipx/uvx installs can run `cs install` without a source checkout.
- Adds replay hook evals and seven public fixtures for common compaction failure modes.
- Adds privacy, threat model, data retention, and security documentation.

## 0.2.0

- Hardens macOS installer with backups, atomic writes, environment checks, safer TOML output, and richer `doctor` diagnostics.
- Fixes checkpoint lifecycle so completing or replacing work closes older active checkpoints.
- Makes hooks fail open on runtime errors instead of disrupting Codex work.
- Uses an empty Stop-hook response for no-op stops and keeps continuation explicitly opt-in.
- Adds clearer GitHub documentation for automatic hooks, optional skill use, MCP tools, limitations, troubleshooting, and verification.

## 0.1.0

- Initial public release.
- Adds Codex Desktop hooks for session start, prompt submit, tool use, tool result, and turn stop.
- Adds local SQLite continuity ledger with checkpoints, notes, event trails, redaction, and loop warnings.
- Adds `compaction-sentinel` skill and MCP tools.
- Adds macOS installer, uninstall script, documentation, and GitHub Actions test workflow.
