# Changelog

## 0.4.6

- Changes the installed skill to prefer the local `~/.codex/bin/cs` CLI for normal checkpoint, evidence, avoid-list, packet, and status writes.
- Keeps MCP tools available, but documents them as secondary for cases where the CLI is unavailable or the user explicitly asks for MCP.
- Explains the Codex Desktop Full Access distinction: shell/filesystem Full Access does not necessarily suppress separate MCP/plugin approval prompts.
- Adds troubleshooting and test coverage so future skill edits do not reintroduce MCP-first behavior for ordinary long-running goal work.

## 0.4.5

- Adds runtime `performance_mode` with `full`, default `balanced`, and `light` settings.
- Adds install-time `--hooks-profile full|balanced|light`; the light profile omits hot `PreToolUse`/`PostToolUse` hooks while keeping startup, prompt, permission, Stop, skill, and MCP continuity.
- Throttles retention cleanup with `retention_check_interval_seconds` and adds `created_at` indexes for events, notes, and checkpoints.
- Throttles per-project pruning with `prune_check_interval_events` so normal event recording does not run COUNT/DELETE maintenance on every hook.
- Makes hot-hook loop checks cheaper: PreToolUse uses same-fingerprint repeat checks, PostToolUse runs failure-loop scans only for failures or test/build commands, and tool-output blindness moves to Stop/full packet contexts.
- Adds compact event storage controls for tool input, normal tool output, and read-only command output.
- Adds clearer CLI failure text when the local SQLite ledger is not writable from the current environment.
- Adds `scripts/benchmark_hooks.py` and a CI smoke run so hook overhead can be measured without making CI depend on timing thresholds.
- Adds slow-session troubleshooting guidance and skill wording to batch evidence at milestones instead of checkpointing after routine reads.

## 0.4.4

- Keeps completed and superseded checkpoints out of `<active_objective>` packets; they can appear only as clearly historical `<last_checkpoint>` context.
- Makes goal inference more conservative so quoted examples like `"set goal:"` inside long prompts do not create bogus active checkpoints.
- Removes Sentinel-owned skill copies during uninstall and preserves non-Sentinel directories with unexpected skill content.
- Extends `doctor` and `doctor --fix` to report and repair missing Sentinel skill copies from installed runtime assets.
- Makes the MCP server honor runtime redaction, packet budget, and loop-threshold settings, with safe parsing for malformed MCP integer arguments.
- Replaces hooks-feature config mutation with exact table/key handling so commented `[features]`, `hooks_extra`, and `plugin_hooks` are not corrupted.
- Adds shell-wrapper failure detection coverage for `bash -lc`, `zsh -lc`, `uv run`, `poetry run`, and `python -m pytest`.
- Adds plugin-shape validation tests for `.codex-plugin/plugin.json`, `.mcp.json`, and `hooks/hooks.json`.
- Updates the skill instructions for explicit MCP checkpoint creation when a long-running task starts with no active checkpoint.

## 0.4.3

- Updates the installed skill to use the guaranteed `~/.codex/bin/cs` path instead of plain `cs`, so agents do not conclude Sentinel is missing when a shell PATH omits `~/.codex/bin`.
- Adds PATH-aware doctor diagnostics: `doctor` now reports where plain `cs` resolves, whether it is Sentinel-owned, and warns when `cs` is missing or shadowed.
- Extends `doctor --fix --explain` with safe PATH guidance instead of reporting a perfect install when only the optional shell shortcut is missing.
- Pins generated CLI launchers to the Python used during install so `~/.codex/bin/cs` does not accidentally run an older system Python from PATH.
- Adds opt-in global shims with `--global-bin /opt/homebrew/bin`; Sentinel records those bins and `uninstall` removes only officially recorded Sentinel-owned global shims.
- Adds installer tests for PATH warnings, non-Sentinel `cs` shadowing, global shim install/repair/uninstall, and skill CLI fallback wording.

## 0.4.2

- Makes PostToolUse failure detection command-aware so read-only `cat`, `sed`, `rg`, `git show`, `json.tool`, and similar commands are not marked failed merely because file contents mention "failed", "exception", or traceback examples.
- Anchors read-only command failures to real command-output errors while still catching validation failures from `jq`, `rg`, and `python -m json.tool`.
- Keeps real failures covered through nonzero structured outcomes, shell/read errors, traceback/error prefixes, and test/build output from pytest, unittest, npm, xcodebuild, Swift, Cargo, Go, and similar commands.
- Stops repeated non-failure PostToolUse results from producing noisy loop warnings; PreToolUse command-loop and investigation-loop guards still catch repeated action patterns.
- Reclassifies existing ledger event categories on upgrade so old false-positive `tool_failure` rows do not keep surfacing in packets.
- Adds unit and replay regression coverage for false-positive doc/source reads versus real repeated test failures.

## 0.4.1

- Adds an internal dogfood report with real project-shape coverage, measured replay outcomes, and explicit limitations.
- Adds tiny-budget packet priority tests for 500, 1000, and 2000 character packets.
- Uses config-safe integer parsing for remaining Stop-continuation loop-threshold and packet-budget paths.
- Hardens packet compaction so tiny packets preserve objective, next action, blockers, strongest evidence, do-not-repeat warning, and resume contract.
- Adds a visible README Known Limitations section.
- Expands the noisy replay budget scenario to 120 tool-result events.

## 0.4.0

- Establishes the Reliability Contract release: correctness and Codex Desktop compatibility over new surface area.
- Requires explicit `cwd` for every MCP tool and fails clearly when missing.
- Updates the skill to always pass `cwd`, record evidence after meaningful tool results, record avoid-list items after repeated failed routes, and verify acceptance criteria plus latest tool output before completion.
- Hardens Stop continuation with session/turn caps, optional per-checkpoint caps, real cooldown enforcement, and hashed project-scoped state keys.
- Replaces broad completion detection with safer positive and negative patterns.
- Makes project scrub remove hashed project-scoped state and legacy raw-path continuation keys.
- Parses `config.toml` with Python `tomllib` in `doctor` instead of string searching.
- Extends replay evals with DB assertions, MCP `cwd`, false-completion, long noisy packet-budget, and state cleanup coverage.
- Removes the global `cs` console-script entry point from package metadata; the installer still creates `~/.codex/bin/cs`.
- Expands CI to Python 3.11, 3.12, and 3.13 on macOS with wheel/package-data/install/repair/uninstall checks.
- Adds plugin privacy and terms URLs.

## 0.3.0

- Adds packet v2 with authority, acceptance criteria, current state, next action, blockers, evidence, do-not-repeat items, and resume contract.
- Adds `PermissionRequest` hook recording without automatic approval or denial.
- Adds regression warnings for repeated commands, repeated failures, investigation loops, stale restarts, revert/oscillation risk, repeated permissions, fake completion, and tool-output blindness.
- Enforces Stop auto-continue caps and cooldown state; auto-continue remains off by default.
- Expands checkpoints with acceptance criteria, files touched, commands run, tests passed/failed, decisions, assumptions, do-not-repeat, last verified time, and confidence.
- Adds CLI/MCP operations for evidence and avoid-list updates.
- Adds `cs scrub`, `cs export`, `cs retention`, `cs config`, `cs backup`, `cs doctor --fix`, `cs doctor --explain`, and `cs uninstall --purge`.
- Adds package assets so pipx/uvx installs can run `compaction-sentinel install` without a source checkout.
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
