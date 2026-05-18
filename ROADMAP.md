# Roadmap

## v0.3.x Hardening

- Expand replay scenarios as real users report compaction drift.
- Add packet quality scoring for objective, evidence, next-action, and budget coverage.
- Add optional anonymized fixture generation from exported local ledgers.
- Improve high-entropy redaction precision with more tests.

## v0.4 Reliability Contract

- Require explicit MCP `cwd` for deterministic project resolution.
- Harden Stop continuation caps, cooldown, completion detection, and scrub behavior.
- Verify package installation and repair paths in CI across Python 3.11, 3.12, and 3.13.

## v0.5 Installer Polish

- Add signed release artifacts if the project grows beyond source/pipx installs.
- Add a guided `cs doctor --fix` dry-run.
- Add optional shell completion.

## v0.6 Public Launch

- Add demo GIFs or screenshots.
- Publish GitHub release notes.
- Add GitHub topics: `codex`, `codex-desktop`, `openai-codex`, `agent-tools`, `agent-memory`, `mcp`, `hooks`, `skills`, `compaction`, `continuity`, `macos`.

## Never Goals

- Do not store full transcripts by default.
- Do not auto-approve permissions.
- Do not enable Stop auto-continue by default.
- Do not claim to make server-side compaction perfect.
