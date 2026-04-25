# Skill Steward

`skill-steward` is an Agent Skill for auditing and managing local AI-agent skills across shared, agent-specific, global, and project-level directories.

It helps agents:

- keep one canonical copy of each skill
- decide whether a skill belongs in `~/.agents/skills` or an agent-specific directory
- audit project-level skills under `.agents/skills`, `.codex/skills`, `.claude/skills`, `.gemini/skills`, or `.cursor/skills`
- converge project layouts around shared and agent-specific skill directories
- create native symlink bridges so agents that only scan their own skills directory can see shared skills without copied duplicates
- configure managed agents from a numbered selection menu with `install`, `add`, `delete`, `set`, and `list`
- scan local session logs for approximate usage and effectiveness signals
- summarize each skill's approximate usage over the last 24 hours, 7 days, and 30 days
- suggest stale, duplicate, or misplaced skills for review

The report mode is non-destructive by default. `--apply-project-layout` intentionally changes project directories to match the canonical hidden layout.

## What It Manages

Use the narrowest directory that still reaches every intended agent:

| Scope | Shared skills | Agent-specific skills |
| --- | --- | --- |
| Global | `~/.agents/skills` | `~/.codex/skills`, `~/.claude/skills`, `~/.gemini/skills`, `~/.cursor/skills` |
| Project | `<repo>/.agents/skills` | `<repo>/.codex/skills`, `<repo>/.claude/skills`, `<repo>/.gemini/skills`, `<repo>/.cursor/skills` |

Put agent-neutral skills in the shared directory. Put runtime-specific skills in the matching agent directory when they depend on Codex, Claude Code, Gemini CLI, Cursor, MCP connector behavior, vendor-only commands, or that agent's prompt-loading rules.

Project-level skills should stay inside the project when they reference local schemas, scripts, infrastructure, or conventions.

## Loader Reality

`skill-steward` does not patch private Codex, Claude Code, Gemini, or Cursor internals. It manages files and instructions around their existing loaders.

When an agent can read shared skill roots from instructions, use `.agents/skills` as the canonical copy. When an agent only scans its own native skills directory, use native bridges:

```text
<repo>/.codex/skills/example -> <repo>/.agents/skills/example
<repo>/.claude/skills/example -> <repo>/.agents/skills/example
```

The bridge is a symlink, not a copied duplicate. The canonical skill remains in `.agents/skills`.

## Install

With Codex, install from the skill subdirectory URL:

```text
$skill-installer install https://github.com/ikingye/skill-steward/tree/main/skill-steward
```

Then restart Codex so the new skill is discovered.

Initialize which agents `skill-steward` should manage:

```bash
python3 ~/.agents/skills/skill-steward/scripts/skill_steward.py install
```

The installer presents Codex, Claude Code, Gemini CLI, and Cursor as numbered choices. Select one or more by number.

For agents that support the Agent Skills folder convention directly, copy or symlink the `skill-steward/` directory into the relevant skills directory, for example:

```bash
mkdir -p ~/.agents/skills
cp -R skill-steward ~/.agents/skills/skill-steward
```

## Recommended Workflows

Ask your agent to use `skill-steward` when you want to audit or reorganize skills.

Example prompts:

```text
Use skill-steward to audit my global skills and recommend duplicates to remove.
```

```text
Use skill-steward to audit this project and decide which skills should be project-level versus global.
```

You can also run the bundled script directly:

```bash
python3 skill-steward/scripts/skill_steward.py --home "$HOME" --days 90
python3 skill-steward/scripts/skill_steward.py --home "$HOME" --project "$PWD" --format json
```

The text report includes a `Usage by Window` table with per-skill counts for the last 24 hours, 7 days, and 30 days. JSON output includes the same data in `usage_windows`:

```json
{
  "name": "skill-steward",
  "last_24h": 12,
  "last_7d": 31,
  "last_30d": 45,
  "by_agent": {
    "codex": {
      "last_24h": 10,
      "last_7d": 24,
      "last_30d": 36
    }
  },
  "last_seen": "2026-04-25T13:38:08.566000+00:00"
}
```

These are approximate mention counts from local logs. When a log line has a timestamp, `skill-steward` uses it; otherwise it falls back to the log file modification time.

### Global Audit

Audit global shared and agent-specific skills:

```bash
python3 skill-steward/scripts/skill_steward.py --home "$HOME" --days 90
```

Create global native bridges after deciding which agents are managed:

```bash
python3 skill-steward/scripts/skill_steward.py --home "$HOME" --apply-native-bridges --bridge-scope global
```

### Managed Agents

Configure managed agents:

```bash
python3 skill-steward/scripts/skill_steward.py list
python3 skill-steward/scripts/skill_steward.py set
python3 skill-steward/scripts/skill_steward.py add
python3 skill-steward/scripts/skill_steward.py delete
```

The commands show numbered choices instead of requiring users to type agent names. Positional agent arguments still work as an automation shortcut, and the explicit form also works: `agents list`, `agents add`, `agents delete`, and `agents set`.

The managed agent list controls which native directories are created or bridged. By default, `skill-steward` manages Codex and Claude Code.

### Project Layout

Apply the canonical project layout:

```bash
python3 skill-steward/scripts/skill_steward.py --project "$PWD" --apply-project-layout
```

You can initialize config and apply a project layout in one command:

```bash
python3 skill-steward/scripts/skill_steward.py install --project "$PWD"
```

This creates `.agents/skills` plus configured agent-specific directories such as `.codex/skills` and `.claude/skills`, and removes identical duplicates from agent-specific directories.

### Native Bridges

For agents whose native loader only scans their own skills directory, create symlink bridges:

```bash
python3 skill-steward/scripts/skill_steward.py --project "$PWD" --apply-native-bridges --bridge-scope project
```

With native bridges, a shared skill remains canonical in `.agents/skills`, while `.codex/skills/<skill>` or `.claude/skills/<skill>` is a symlink to the shared skill. This is a loader bridge, not a copied duplicate. If a same-name agent-specific skill already exists with different content, `skill-steward` stops instead of overwriting it.

Use `--bridge-scope project`, `--bridge-scope global`, or `--bridge-scope both` to control where bridges are created. Be explicit in automation so a project-only operation does not also touch global directories.

### What Changes Files

These modes mutate the filesystem:

- `install`, `add`, `delete`, and `set` update `~/.config/skill-steward/config.json` unless `--config` is supplied.
- `--apply-project-layout` creates hidden project skill directories, writes managed instruction blocks, and removes identical duplicates.
- `--apply-native-bridges` creates or refreshes symlink bridges from shared skills into managed native agent directories.

Plain audit commands without `--apply-*` only report findings.

## Repository Layout

```text
skill-steward/
  SKILL.md
  LICENSE.txt
  agents/openai.yaml
  references/directory-principles.md
  scripts/skill_steward.py
tests/
  test_skill_steward.py
```

The installable skill package is the `skill-steward/` subdirectory. Repository-level files such as this README, CI, and tests are not part of the runtime skill package.

## Development

Run tests:

```bash
python3 -m unittest discover -s tests
```

Validate the skill package:

```bash
python3 path/to/quick_validate.py skill-steward
```

This project uses only Python's standard library.
