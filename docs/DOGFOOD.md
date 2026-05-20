# Dogfood Results

This is an internal dogfood report for the v0.4 Reliability Contract, v0.4.1 public-proof follow-up, v0.4.2 false-positive failure-detection hardening, v0.4.3 CLI discoverability hardening, v0.4.4 install/MCP/packet reliability hardening, and v0.4.5 performance hardening. It is not an independent benchmark and it does not claim to control OpenAI server-side compaction. It measures whether Sentinel preserves the local continuity contract around Codex Desktop: objective, next action, evidence, loop warnings, redaction, project resolution, and hot-hook overhead.

## Real Project Surfaces

Checked on May 18-19, 2026 from the same Mac that runs Codex Desktop:

| Surface | Local proof | Result |
| --- | --- | --- |
| Python CLI/package | Compaction Sentinel itself, `pyproject.toml`, wheel build, temp install, real `~/.codex` install | Passed |
| React/Electron app | `X Growth Lab`, `package.json`, installed app bundle resources | `~/.codex/bin/cs packet --cwd` resolved the project |
| iOS/Capacitor app | `emerald-singularity`, `package.json`, `capacitor.config.ts` | `~/.codex/bin/cs packet --cwd` resolved the project |
| Large Unity repo | `OFFDuty-codex-phase7-tps-rig`, `ProjectSettings` | `~/.codex/bin/cs packet --cwd` resolved the project |

## Measured Outcomes

| Claim | Measurement | Result |
| --- | --- | --- |
| Project-scoped state does not drift across repos | MCP and CLI require explicit `cwd`; local project-shape smoke covered 4 project types | 4/4 |
| Stale restart avoided | Replay fixture restores the active objective and next action instead of restarting from the last user message | 1/1 |
| Repeated command loops caught | Replay fixture repeats the same failing test route until the loop warning fires | 1/1 |
| Next action preserved after resume | Stale-restart and project-switching fixtures preserve the intended next action | 3/3 |
| False completion caught | False-completion Stop replay and tool-output-blindness replay prevent treating failed output as finished work | 2/2 |
| Secrets leaked | Redaction replay asserts raw GitHub, AWS, and Slack tokens are absent from packet and SQLite text | 0 raw secret leaks observed |
| Tiny packet budgets preserve priority | Unit tests assert 500, 1000, and 2000 char packets keep objective, next action, blocker, strongest evidence, do-not-repeat warning, and resume contract | 3/3 |
| Large noisy runs stay compact | Replay stress records 120 noisy tool results and keeps the packet under budget | 120 events |
| False failure avoided | Replay fixture repeats doc/source reads whose contents mention failure words without producing a failure-loop warning | 1/1 |
| CLI fallback avoids PATH drift | Installer tests verify `~/.codex/bin/cs` guidance, PATH warnings, non-Sentinel `cs` shadowing, and opt-in global shim cleanup | 4/4 |

## Real Release Run

The v0.4 release itself was dogfooded as a long Codex Desktop task:

- Sentinel was installed into the real `~/.codex` home.
- The task used Sentinel checkpoints and evidence updates while implementing, verifying, committing, pushing, and watching CI.
- Local proof included compile, unit tests, replay evals, wheel build, package-data check, temp install, doctor repair, uninstall purge, real install, MCP listing, and GitHub Actions.
- GitHub Actions passed on macOS with Python 3.11, 3.12, and 3.13.

## What This Does Not Prove Yet

- It is not a randomized third-party study.
- It does not prove OpenAI server-side compaction can never lose useful state.
- It does not prove hook interception covers every possible tool path.
- It does not prove auto-continue is safe for every project; auto-continue remains off by default.

The next credibility step is to collect more anonymized user-submitted dogfood reports using `~/.codex/bin/cs export --project --cwd "$PWD"` output with secrets scrubbed.
