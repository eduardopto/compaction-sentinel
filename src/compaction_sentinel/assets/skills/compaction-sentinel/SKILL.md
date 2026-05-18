---
name: compaction-sentinel
description: Preserve active Codex Desktop work across compaction, resumes, long unattended runs, and repeated loops. Use when a task mentions compaction, continuity, "do not stop", loop prevention, durable checkpoints, or long multi-step work.
---

# Compaction Sentinel

Use this skill when the user needs Codex to survive compaction or resume a long-running task without forgetting the live process.

When Compaction Sentinel is installed, hooks should already inject a resume packet automatically. The skill is extra behavior guidance, not the only activation path.

## Operating Rules

1. Treat the latest Compaction Sentinel packet, current files, and explicit user acceptance criteria as the resume authority.
2. Continue the active objective. Do not restart from only the last user message after compaction.
3. Before repeating a command, proof pass, investigation route, or fix, inspect the latest concrete artifact or log and choose one changed hypothesis.
4. Write a checkpoint whenever the current step, next action, blocker, acceptance criteria, or verified evidence changes.
5. Keep checkpoints factual and compact: objective, acceptance criteria, current step, next action, blockers, evidence, files touched, commands run, tests passed/failed, decisions, assumptions, and do-not-repeat items when relevant.
6. If the packet and repository disagree, verify the repository and update the checkpoint.
7. Do not treat a loop warning as proof of failure; inspect the latest concrete output and then choose a different hypothesis.

## MCP Tools

When available, prefer the `compaction_sentinel` MCP tools:

- `compaction_checkpoint`: save a durable checkpoint.
- `compaction_note`: save a durable note that should survive compaction.
- `compaction_packet`: fetch the current resume packet.
- `compaction_search`: search recent recorded events.
- `compaction_status`: inspect the current state.

## CLI Fallback

If MCP tools are unavailable but the runtime is installed, use:

```bash
cs checkpoint --objective "..." --current-step "..." --next-action "..." --evidence "..."
cs checkpoint --same-objective --current-step "..." --next-action "..." --tests-passed "..."
cs evidence add "make test passed"
cs avoid add "Do not rerun npm install; dependency issue was already ruled out"
cs note "..." --when "..."
cs packet
cs status
cs doctor
```

## Checkpoint Quality

Good checkpoint:

```text
objective: Fix account deletion and install on device.
current_step: App-side cleanup is implemented; native sync passed.
next_action: Build and install to the connected iPhone, then report locked-device launch separately.
blockers: Phone may be locked at launch.
evidence: cap sync succeeded; xcodebuild archive succeeded.
do_not_repeat: Do not rerun account-deletion unit tests without first changing the failing API hypothesis.
```

Weak checkpoint:

```text
Working on it.
```

## Stop Rule

Never claim completion because a checkpoint exists. Completion requires the user-visible finish line to be achieved and verified.
