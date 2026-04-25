---
name: skill-steward
description: Use when managing local or project agent skills across shared and agent-specific directories, deduplicating common skills, deciding whether a skill belongs in shared or Codex/Claude/Gemini/Cursor-specific locations, auditing skill usage, or pruning stale skills.
---

# Skill Steward

## Overview

Use one canonical copy of each skill. Put agent-neutral skills in shared directories and keep agent/runtime-specific skills in that agent's own directory.

This skill is non-destructive by default: inventory first, rank usage, then recommend moves, deduplication, or pruning. Delete or move files only after reviewing the report.

For project layout convergence, use the bundled script with `--apply-project-layout`. That mode intentionally changes directories: it migrates legacy top-level `skills/` into `.agents/skills`, creates configured agent-specific directories, removes identical agent-specific duplicates, and writes project loader instructions.

For native loaders that only scan one agent-specific skills directory, use `--apply-native-bridges`. That creates symlinks from agent-specific directories back to canonical shared skills, without copying skill contents.

## Directory Model

Global shared skills:
- `~/.agents/skills`

Global agent-specific skills:
- Codex: `${CODEX_HOME:-~/.codex}/skills`
- Claude Code: `~/.claude/skills`
- Gemini CLI: `~/.gemini/skills`
- Cursor CLI/editor agents: `~/.cursor/skills`

Project shared skills:
- `<repo>/.agents/skills`

Project agent-specific skills:
- `<repo>/.codex/skills`
- `<repo>/.claude/skills`
- `<repo>/.gemini/skills`
- `<repo>/.cursor/skills`

Use shared when the skill describes a reusable workflow, mental model, domain process, or tool-independent policy. Use agent-specific when the skill depends on that agent's tool names, runtime, MCP connector behavior, prompt loading rules, or vendor-only commands.

Read `references/directory-principles.md` when configuring agents to read the shared directory or when writing project-level loader instructions.

## Managed Agents

Configure which agent-specific project directories `skill-steward` manages:

```bash
python3 scripts/skill_steward.py install
python3 scripts/skill_steward.py list
python3 scripts/skill_steward.py set
python3 scripts/skill_steward.py add
python3 scripts/skill_steward.py delete
```

These commands present numbered choices for Codex, Claude Code, Gemini CLI, and Cursor. Positional agent arguments remain available for automation. The explicit `agents list/add/delete/set` subcommands are also supported.

The config is stored at `~/.config/skill-steward/config.json` by default. Override it with `--config /path/to/config.json` or `SKILL_STEWARD_CONFIG`.

Default managed agents are `codex` and `claude`.

## Audit Workflow

1. Run the inventory report from this skill folder:

```bash
python3 scripts/skill_steward.py --home "$HOME" --days 90
```

2. Include a project root when auditing project-level skills:

```bash
python3 scripts/skill_steward.py --home "$HOME" --project "$PWD" --days 90
```

3. Apply the canonical hidden project layout:

```bash
python3 scripts/skill_steward.py --project "$PWD" --apply-project-layout
```

Use the configured managed agents for normal runs. Use `python3 scripts/skill_steward.py install --project "$PWD"` to choose managed agents and apply the layout in one command.

To make native agent loaders see shared skills through their own directories, create bridges:

```bash
python3 scripts/skill_steward.py --project "$PWD" --apply-native-bridges
```

The bridge is a symlink such as `.claude/skills/example -> ../../.agents/skills/example`, not a copied duplicate. If a conflicting same-name native skill exists, the script stops for manual review.
Use `--bridge-scope project`, `--bridge-scope global`, or `--bridge-scope both` to avoid changing more than the intended scope.

4. Use JSON for automation or a saved baseline:

```bash
python3 scripts/skill_steward.py --home "$HOME" --project "$PWD" --format json > skill-report.json
```

Use HTML for a local static report:

```bash
python3 scripts/skill_steward.py --home "$HOME" --format html > skill-report.html
```

5. Review recommendations in this order:
- `deduplicate`: same skill name exists in multiple roots. Keep one canonical copy.
- `move-to-shared`: an agent-specific skill looks agent-neutral.
- `move-to-agent-specific`: a shared skill mentions one agent or vendor specifically.
- `review-effectiveness`: nearby log signals skew negative.
- `review-or-prune`: no usage mentions were found in scanned logs.

6. Before deleting anything, confirm the surviving directory is readable by every intended agent. Prefer moving to a quarantine folder first when usage is uncertain.

## Usage Metrics

The script scans known local session/history roots plus any `--log-root` paths. It counts exact skill-name mentions by agent and computes an approximate effectiveness score from nearby positive and negative terms.

The text report includes a `Usage by Window` table for each skill with approximate counts over the last 24 hours, 7 days, and 30 days. JSON output exposes the same data as `usage_windows`, including per-agent breakdowns.

When log lines contain timestamps, the script uses those timestamps for windowed counts. Otherwise it falls back to the log file modification time.

The report also includes `Usage Confidence`, exposed as `usage_confidence` in JSON. It separates:
- `used`: strong evidence such as `Using <skill>`, `Loaded <skill>`, `Invoked <skill>`, or structured `event=used` logs.
- `likely`: explicit intent such as `Use <skill>` or structured request/recommendation logs.
- `mention`: the skill name appears but the line does not prove usage.

Use `actual_or_likely_uses`, `mentions`, `event_type`, `confidence`, `success_signals`, and `failure_signals` together. `confidence` weights strong signals as `1.0`, likely signals as `0.7`, and mention-only signals as `0.2`.

Record explicit usage events when a wrapper, agent instruction, or user workflow knows a skill was used:

```bash
python3 scripts/skill_steward.py event used <skill-name> --agent codex --outcome success
python3 scripts/skill_steward.py event likely <skill-name> --agent claude
```

Events are appended to `~/.agents/skill-steward/events.jsonl` and included in future audits automatically.

Treat these as triage signals, not truth. A high count may include discussions about a skill rather than real activation. A low count may mean an agent does not log skill use explicitly. Use the ranking to choose what to inspect manually.

## Quality Checks

Check skill quality before publishing or pruning:

```bash
python3 scripts/skill_steward.py skills quality
python3 scripts/skill_steward.py skills quality --project "$PWD" --format json
```

The quality report assigns each skill a score from 0 to 100 and flags broad descriptions, hardcoded local absolute paths, non-executable shebang scripts, and shared versus agent-specific placement drift. Treat these findings as review prompts; fix the skill text or move the skill before deleting it.

## Safe Cleanup Rules

- Never keep the same shared skill copied into multiple agent-specific directories just to make loading work; configure the agent loader instead.
- Use native symlink bridges when an agent has no public setting for extra skill roots but can resolve skills from its own native directory.
- Do not leave a top-level project `skills/` directory or symlink as a compatibility bridge. Migrate project skills into `.agents/skills`.
- Do not move bundled/system skills unless you own them and understand their loader behavior.
- Do not delete a skill solely because it has zero observed mentions; first check whether logs are available for that agent.
- When two copies differ, compare `SKILL.md`, scripts, and references before choosing the canonical copy.
- For project-only conventions, prefer project shared or project agent-specific directories over global directories.

## Cleanup Commands

Generate a cleanup plan first:

```bash
python3 scripts/skill_steward.py skills cleanup-plan
python3 scripts/skill_steward.py skills cleanup-plan --format json
```

The plan groups skills as `safe-to-remove`, `review-manually`, `keep`, or `protected-or-skip`. The default `safe-to-remove` rule is conservative: no actual or likely uses, only weak mentions, low confidence, and last seen at least 30 days ago. Use `--stale-days <days>` to tune the age threshold.

Prefer reversible quarantine before deletion:

```bash
python3 scripts/skill_steward.py skills quarantine <skill-name>
python3 scripts/skill_steward.py skills list-trash
python3 scripts/skill_steward.py skills restore <skill-name>
```

Quarantine moves the skill into `~/.agents/.trash/skills/<timestamp>-<skill>/`, writes a manifest, and removes native bridge symlinks that point at the canonical skill. Restore moves the skill back and recreates those bridges.

Permanent deletion requires explicit confirmation:

```bash
python3 scripts/skill_steward.py skills delete <skill-name> --yes
```

Protected runtime skills are skipped. Pass `--project <repo>` when cleaning project-level skills. Use `--format json` for automation.

## Script Notes

`scripts/skill_steward.py` uses only Python's standard library. It defaults to the tail of recent log files for speed and skips heavy cache, plugin, extension, virtualenv, and repository directories.

Pass `--log-root <path>` repeatedly when an agent stores logs somewhere unusual.
