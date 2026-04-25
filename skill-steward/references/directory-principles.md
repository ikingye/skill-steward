# Directory Principles

## Canonical Placement

Use the narrowest directory that still reaches every intended agent.

| Scope | Shared | Agent-specific |
| --- | --- | --- |
| Global | `~/.agents/skills` | `~/.codex/skills`, `~/.claude/skills`, `~/.gemini/skills`, `~/.cursor/skills` |
| Project | `<repo>/.agents/skills` | `<repo>/.codex/skills`, `<repo>/.claude/skills`, `<repo>/.gemini/skills`, `<repo>/.cursor/skills` |

Do not keep a top-level `<repo>/skills` directory or symlink. If it exists, migrate it into `<repo>/.agents/skills` and remove the top-level path.

Prefer shared when:
- The workflow is agent-neutral.
- The skill does not require a specific agent's tool names, connector metadata, or prompt-loading behavior.
- Multiple agents should use the same policy or operating principle.

Prefer agent-specific when:
- The skill names agent-only tools or system behaviors.
- The skill depends on a vendor-specific API, MCP connector, or command wrapper.
- The skill should trigger for one agent and would mislead another.

Prefer project-level when:
- The behavior only makes sense inside one repository.
- The skill references project schemas, scripts, infrastructure, or local conventions.
- Global installation would add noise for unrelated work.

## Loader Snippets

Put the same principle in each agent's global instruction file, adapted to that agent's format:

```markdown
Shared skills live in ~/.agents/skills. Agent-specific skills live in this agent's own skills directory. Prefer the shared copy when a skill is agent-neutral; do not duplicate shared skills into agent-specific directories just to make discovery work.
```

Common locations:
- Codex: `~/.codex/AGENTS.md`
- Claude Code: `~/.claude/CLAUDE.md`
- Gemini CLI: `~/.gemini/GEMINI.md`
- Cursor: global or project rules files used by Cursor agents

For projects, add the same rule to the repo's `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, or Cursor rules file:

```markdown
Project shared skills live in .agents/skills. Agent-specific project skills live in .codex/skills, .claude/skills, .gemini/skills, or .cursor/skills. Keep only one canonical copy of each skill.
```

`skill-steward` can write managed blocks for Codex, Claude Code, and Gemini CLI. Configure managed agents first:

```bash
python3 scripts/skill_steward.py install
python3 scripts/skill_steward.py add
python3 scripts/skill_steward.py delete
```

Each command presents a numbered selection menu for supported agents.

Then apply the layout:

```bash
python3 scripts/skill_steward.py --project <repo> --apply-project-layout
```

Or initialize the agent list and apply the layout together:

```bash
python3 scripts/skill_steward.py install --project <repo>
```

## Native Loader Bridges

Some agents expose a native skills directory but do not expose a public setting for additional shared skill roots. In that case, prefer symlink bridges over copied duplicates:

```bash
python3 scripts/skill_steward.py --project <repo> --apply-native-bridges
```

This creates entries such as:

```text
<repo>/.claude/skills/example -> <repo>/.agents/skills/example
<repo>/.codex/skills/example -> <repo>/.agents/skills/example
```

The canonical skill still lives in `.agents/skills`; the agent-specific directory only provides native loader visibility. If an agent-specific same-name skill exists with different content, stop and resolve the conflict manually.

Use `--bridge-scope project`, `--bridge-scope global`, or `--bridge-scope both` to limit the change. For example, when testing one repository, prefer `--bridge-scope project`.

## Deduplication Policy

When the same skill name appears in multiple places:

1. If one copy is in shared and the others are identical agent-specific copies, keep shared and remove or archive the duplicates.
2. If the shared copy is actually agent-specific, move it to that agent's directory.
3. If copies differ, compare `SKILL.md` plus bundled scripts/references before merging.
4. If a skill is bundled, generated, or under a protected system directory, report it but avoid moving it automatically.

## Pruning Policy

Use usage data to prioritize review, not as the only deletion criterion.

Prune candidates:
- No observed usage in the selected time window.
- Low effectiveness score plus repeated negative signals.
- Overlapping responsibilities with a more frequently used skill.
- Skills whose trigger descriptions are too broad and interfere with more precise skills.

Keep candidates:
- Infrequent but high-value skills for rare file formats, compliance tasks, incident response, or specialized workflows.
- Skills that do not log cleanly but are known to be used.
- System or vendor-provided skills unless intentionally overriding them.
