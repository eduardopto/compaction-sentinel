# Threat Model

Compaction Sentinel is a local developer productivity tool. Its main risk is not remote code execution; it is accidentally preserving or surfacing sensitive local context.

## Assets

- Local project paths.
- Compact summaries of prompts and tool activity.
- Checkpoints and notes written by Codex or the user.
- Install files under `~/.codex`.

## Trust Boundaries

- Codex Desktop invokes hooks and passes JSON payloads.
- Sentinel writes local SQLite and config files.
- MCP exposes Sentinel tools to Codex.
- The installer edits user-level Codex config.

## In Scope

- Avoid storing full transcripts by default.
- Redact common secrets before writing hook summaries.
- Fail open when hooks crash.
- Never auto-approve `PermissionRequest`.
- Make install/uninstall reversible through backups.
- Keep auto-continue opt-in and capped.

## Out Of Scope

- Preventing every possible malicious prompt or tool path.
- Replacing Codex sandboxing or approval UX.
- Acting as a complete security enforcement boundary.
- Protecting against an attacker who already has full local account access.

## Key Controls

- User-level hooks are explicit install artifacts.
- Plugin hooks are documented as opt-in, not assumed automatic.
- Permission requests are recorded but not decided.
- `cs scrub` and `cs export` give users data control.
- `cs doctor --fix` repairs known config drift without wiping the ledger.
- `cs backup restore` rolls back installer config changes.

## Residual Risks

- Redaction is pattern-based and can miss novel secret formats.
- Hook payload schemas may change.
- A local process with user privileges can read the SQLite database.
- A very long packet can still compete with task context, so packet budget tests remain part of verification.
