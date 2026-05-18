# Changelog

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
