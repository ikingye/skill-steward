#!/usr/bin/env python3
"""Inventory and usage reporting for shared and agent-specific skills."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


AGENT_DIRS = {
    "shared": ".agents/skills",
    "codex": ".codex/skills",
    "claude": ".claude/skills",
    "gemini": ".gemini/skills",
    "cursor": ".cursor/skills",
}

PROJECT_AGENT_FILES = {
    "codex": "AGENTS.md",
    "claude": "CLAUDE.md",
    "gemini": "GEMINI.md",
}

DEFAULT_PROJECT_AGENTS = ("codex", "claude")
AGENT_CHOICES = ("codex", "claude", "gemini", "cursor")
AGENT_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "gemini": "Gemini CLI",
    "cursor": "Cursor",
}
AGENT_ALIASES = {
    "codex": "codex",
    "codex-cli": "codex",
    "claude": "claude",
    "claude-code": "claude",
    "claude-cli": "claude",
    "gemini": "gemini",
    "gemini-cli": "gemini",
    "cursor": "cursor",
    "cursor-cli": "cursor",
}
BRIDGE_SCOPES = ("global", "project", "both")
MANAGED_BLOCK_START = "<!-- skill-steward project skills start -->"
MANAGED_BLOCK_END = "<!-- skill-steward project skills end -->"
LEGACY_LOADER_PATTERN = re.compile(
    r"(^|\n)# Project Skills\n\n"
    r"Project shared skills live in `\.agents/skills`\..*?"
    r"Do not duplicate the same skill name in both directories\.[^\n]*(?:\n|$)",
    re.DOTALL,
)
CONFIG_ENV = "SKILL_STEWARD_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/skill-steward/config.json")
TRASH_ROOT_REL = Path(".agents/.trash/skills")
EVENT_LOG_REL = Path(".agents/skill-steward/events.jsonl")
POLICY_FILENAMES = (".skill-steward.json", "skill-steward.json")

PROTECTED_PARTS = {".system", "codex-primary-runtime", ".builtin", ".curated"}

AGENT_KEYWORDS = {
    "codex": ("codex", "codex cli", "codex_home", "agents.md", "sandbox_permissions"),
    "claude": ("claude", "claude code", "claude_skill_dir", "claude.md", ".claude/skills"),
    "gemini": ("gemini",),
    "cursor": ("cursor",),
}

POSITIVE_TERMS = (
    "pass",
    "passed",
    "passing",
    "success",
    "successful",
    "succeeded",
    "complete",
    "completed",
    "fixed",
    "verified",
    "validated",
)

NEGATIVE_TERMS = (
    "fail",
    "failed",
    "failing",
    "failure",
    "error",
    "blocked",
    "regression",
    "broken",
    "timeout",
)

TEXT_SUFFIXES = {".jsonl", ".json", ".log", ".txt", ".md", ".yaml", ".yml"}
MAX_LOG_BYTES = 100 * 1024 * 1024
READ_TAIL_BYTES = 256 * 1024
USAGE_WINDOWS = (("last_24h", "24h", 1), ("last_7d", "7d", 7), ("last_30d", "30d", 30))
TIMESTAMP_KEYS = ("timestamp", "time", "created_at", "createdAt", "date", "ts")
STRUCTURED_SKILL_KEYS = ("skill", "skill_name", "skillName")
STRUCTURED_EVENT_KEYS = ("event", "action", "type")
USED_EVENTS = {"use", "used", "using", "load", "loaded", "invoke", "invoked", "activate", "activated", "trigger", "triggered"}
LIKELY_EVENTS = {"likely", "request", "requested", "recommend", "recommended", "suggest", "suggested", "select", "selected"}
SIGNAL_WEIGHTS = {"used": 1.0, "likely": 0.7, "mention": 0.2}
ISO_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?"
)
SKIP_LOG_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "cache",
    "extensions",
    "node_modules",
    "plugins",
    "skills",
    "vendor_imports",
}
BROAD_DESCRIPTION_TERMS = (
    "doing anything",
    "anything",
    "everything",
    "all tasks",
    "any task",
    "general help",
    "helping users",
    "various tasks",
)
HARDCODED_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:/Users|/home|/var/folders)/[^\s`'\"<>)]*"
)


@dataclass(frozen=True)
class SkillRoot:
    agent: str
    scope: str
    path: Path


class LayoutError(RuntimeError):
    """Raised when a project layout cannot be changed safely."""


def parse_frontmatter(skill_file: Path) -> dict[str, str]:
    text = skill_file.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    values: dict[str, str] = {}
    lines = parts[1].splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if ":" not in line:
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in {"name", "description"}:
            if value.startswith("|") or value.startswith(">"):
                index += 1
                block_lines: list[str] = []
                while index < len(lines):
                    block_line = lines[index]
                    if block_line.strip() == "":
                        block_lines.append("")
                        index += 1
                        continue
                    if not block_line.startswith((" ", "\t")):
                        break
                    block_lines.append(block_line.lstrip())
                    index += 1
                text = "\n".join(block_lines).strip()
                if value.startswith(">"):
                    text = " ".join(part.strip() for part in text.splitlines())
                values[key] = text
                continue
            values[key] = value.strip("\"'")
        index += 1
    return values


def skill_hash(skill_file: Path) -> str:
    return hashlib.sha256(skill_file.read_bytes()).hexdigest()


def is_protected(path: Path) -> bool:
    return any(part in PROTECTED_PARTS for part in path.parts)


def detect_agent_mentions(text: str) -> set[str]:
    haystack = text.lower()
    matches = set()
    for agent, keywords in AGENT_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            matches.add(agent)
    return matches


def detect_agent_specific_agent(text: str) -> str | None:
    matches = detect_agent_mentions(text)
    if len(matches) == 1:
        return next(iter(matches))
    return None


def skill_roots(home: Path, project: Path | None = None) -> list[SkillRoot]:
    roots: list[SkillRoot] = []
    for agent, rel in AGENT_DIRS.items():
        roots.append(SkillRoot(agent=agent, scope="global", path=home / rel))
    if project is not None:
        for agent, rel in AGENT_DIRS.items():
            roots.append(SkillRoot(agent=agent, scope="project", path=project / rel))
    return roots


def iter_skill_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_LOG_DIRS and not name.startswith(".venv")]
        if "SKILL.md" in filenames:
            yield Path(dirpath) / "SKILL.md"


def discover_skills(home: Path, project: Path | None = None) -> tuple[list[dict], list[dict]]:
    roots = skill_roots(home, project)
    skills: list[dict] = []
    root_rows: list[dict] = []
    scanned_real_roots: dict[Path, SkillRoot] = {}

    for root in roots:
        exists = root.path.exists()
        row = {
            "agent": root.agent,
            "scope": root.scope,
            "path": str(root.path),
            "exists": exists,
        }
        if not exists:
            root_rows.append(row)
            continue

        real_root = root.path.resolve()
        if real_root in scanned_real_roots:
            first = scanned_real_roots[real_root]
            row["alias_of"] = str(first.path)
            row["resolved_path"] = str(real_root)
            root_rows.append(row)
            continue

        scanned_real_roots[real_root] = root
        row["resolved_path"] = str(real_root)
        root_rows.append(row)

        for skill_file in sorted(iter_skill_files(root.path)):
            skill_text = skill_file.read_text(encoding="utf-8", errors="replace")
            metadata = parse_frontmatter(skill_file)
            folder = skill_file.parent.name
            name = metadata.get("name") or folder
            description = metadata.get("description", "")
            metadata_text = f"{name} {description}"
            rel = skill_file.parent.relative_to(root.path)
            protected = is_protected(skill_file.parent)
            metadata_agent_mentions = sorted(detect_agent_mentions(metadata_text))
            content_agent_mentions = sorted(detect_agent_mentions(skill_text))
            desired_agent = detect_agent_specific_agent(metadata_text)
            skills.append(
                {
                    "name": name,
                    "folder": folder,
                    "description": description,
                    "agent": root.agent,
                    "scope": root.scope,
                    "path": str(skill_file.parent),
                    "relative_path": str(rel),
                    "hash": skill_hash(skill_file),
                    "protected": protected,
                    "desired_agent": desired_agent,
                    "metadata_agent_mentions": metadata_agent_mentions,
                    "content_agent_mentions": content_agent_mentions,
                }
            )

    skills.sort(key=lambda item: (item["name"], item["scope"], item["agent"], item["path"]))
    return skills, root_rows


def duplicate_report(skills: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for skill in skills:
        grouped[skill["name"]].append(skill)

    duplicates: list[dict] = []
    for name, rows in grouped.items():
        if len(rows) < 2:
            continue
        duplicates.append(
            {
                "name": name,
                "same_content": len({row["hash"] for row in rows}) == 1,
                "locations": [
                    {
                        "agent": row["agent"],
                        "scope": row["scope"],
                        "path": row["path"],
                        "protected": row["protected"],
                    }
                    for row in rows
                ],
            }
        )
    return sorted(duplicates, key=lambda item: item["name"])


def skill_dirs(root: Path, include_symlinks: bool = False) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and (include_symlinks or not path.is_symlink()) and (path / "SKILL.md").is_file()
    )


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def same_skill_content(left: Path, right: Path) -> bool:
    left_files = sorted(path.relative_to(left) for path in left.rglob("*") if path.is_file())
    right_files = sorted(path.relative_to(right) for path in right.rglob("*") if path.is_file())
    if left_files != right_files:
        return False
    return all(file_hash(left / rel) == file_hash(right / rel) for rel in left_files)


def record_action(actions: list[dict], action: str, **fields: str) -> None:
    row = {"action": action}
    row.update(fields)
    actions.append(row)


def config_file(config_path: Path | None = None) -> Path:
    if config_path is not None:
        return config_path.expanduser()
    configured = os.environ.get(CONFIG_ENV)
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_CONFIG_PATH.expanduser()


def validate_agents(agents: Iterable[str]) -> list[str]:
    normalized = []
    for agent in agents:
        key = re.sub(r"[\s_]+", "-", agent.strip().lower())
        normalized.append(AGENT_ALIASES.get(key, key))
    normalized = list(dict.fromkeys(normalized))
    invalid = [agent for agent in normalized if agent not in AGENT_CHOICES]
    if invalid:
        raise LayoutError(f"unknown agent(s): {', '.join(invalid)}")
    return normalized


def load_config(config_path: Path | None = None) -> dict:
    path = config_file(config_path)
    if not path.exists():
        return {"managed_agents": list(DEFAULT_PROJECT_AGENTS)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LayoutError(f"invalid config JSON at {path}: {exc}") from exc
    agents = data.get("managed_agents", list(DEFAULT_PROJECT_AGENTS))
    if not isinstance(agents, list) or not all(isinstance(agent, str) for agent in agents):
        raise LayoutError(f"config managed_agents must be a list of strings: {path}")
    data["managed_agents"] = validate_agents(agents)
    return data


def save_config(config: dict, config_path: Path | None = None) -> None:
    path = config_file(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_policy_path(project: Path | None = None, policy_path: Path | None = None) -> Path | None:
    if policy_path is not None:
        return policy_path.expanduser().resolve()
    if project is None:
        return None
    for filename in POLICY_FILENAMES:
        candidate = project / filename
        if candidate.is_file():
            return candidate
    return None


def load_policy(project: Path | None = None, policy_path: Path | None = None) -> tuple[dict, Path | None]:
    path = resolve_policy_path(project, policy_path)
    if path is None:
        return {}, None
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LayoutError(f"invalid policy JSON at {path}: {exc}") from exc
    if not isinstance(policy, dict):
        raise LayoutError(f"policy must be a JSON object: {path}")
    return policy, path


def list_managed_agents(config_path: Path | None = None) -> list[str]:
    return load_config(config_path).get("managed_agents", list(DEFAULT_PROJECT_AGENTS))


def add_managed_agents(agents: Iterable[str], config_path: Path | None = None) -> list[str]:
    config = load_config(config_path)
    managed = validate_agents([*config.get("managed_agents", []), *agents])
    config["managed_agents"] = managed
    save_config(config, config_path)
    return managed


def delete_managed_agents(agents: Iterable[str], config_path: Path | None = None) -> list[str]:
    remove = set(validate_agents(agents))
    config = load_config(config_path)
    managed = [agent for agent in config.get("managed_agents", []) if agent not in remove]
    config["managed_agents"] = managed
    save_config(config, config_path)
    return managed


def set_managed_agents(agents: Iterable[str], config_path: Path | None = None) -> list[str]:
    managed = validate_agents(agents)
    config = load_config(config_path)
    config["managed_agents"] = managed
    save_config(config, config_path)
    return managed


def parse_agent_selection(text: str, choices: list[str]) -> list[str] | None:
    value = text.strip().lower()
    if not value:
        return None
    if value in {"all", "a", "*"}:
        return choices
    if value in {"none", "no", "n", "-"}:
        return []

    selected: list[str] = []
    for token in re.split(r"[,\s]+", value):
        if not token:
            continue
        if "-" in token:
            start, end = token.split("-", 1)
            if not start.isdigit() or not end.isdigit():
                raise LayoutError(f"invalid selection: {token}")
            start_index = int(start)
            end_index = int(end)
            if start_index > end_index:
                raise LayoutError(f"invalid selection range: {token}")
            indexes = range(start_index, end_index + 1)
        elif token.isdigit():
            indexes = [int(token)]
        else:
            raise LayoutError(f"invalid selection: {token}")

        for index in indexes:
            if index < 1 or index > len(choices):
                raise LayoutError(f"selection out of range: {index}")
            agent = choices[index - 1]
            if agent not in selected:
                selected.append(agent)
    return selected


def choose_agents(
    title: str,
    choices: Iterable[str],
    default: Iterable[str] | None = None,
    allow_empty: bool = False,
) -> list[str]:
    available = list(choices)
    if not available:
        return []

    default_list = validate_agents(default or [])
    default_list = [agent for agent in default_list if agent in available]
    default_text = ", ".join(str(available.index(agent) + 1) for agent in default_list)
    prompt = "Choose numbers"
    if len(available) > 1:
        prompt += " separated by commas"
    if default_list:
        prompt += f" [default: {default_text}]"
    elif allow_empty:
        prompt += " [Enter for none]"
    prompt += ": "

    while True:
        print(title)
        for index, agent in enumerate(available, start=1):
            marker = " (default)" if agent in default_list else ""
            print(f"  {index}. {AGENT_LABELS[agent]} ({agent}){marker}")
        try:
            selected = parse_agent_selection(input(prompt), available)
        except EOFError as exc:
            if default_list or allow_empty:
                return default_list
            raise LayoutError("no agent selection provided") from exc
        except LayoutError as exc:
            print(f"{exc}", file=sys.stderr)
            continue

        if selected is None:
            selected = default_list
        if selected or allow_empty:
            return selected
        print("Select at least one agent.", file=sys.stderr)


def strip_legacy_loader_text(text: str) -> str:
    def replacement(match: re.Match) -> str:
        return "\n" if match.group(1) else ""

    stripped = LEGACY_LOADER_PATTERN.sub(replacement, text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return f"{stripped}\n" if stripped else ""


def append_managed_block(base: str, managed: str) -> str:
    prefix = "" if not base or base.endswith("\n") else "\n"
    return base + prefix + ("\n" if base else "") + managed


def ensure_managed_block(path: Path, block: str, actions: list[dict]) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    managed = f"{MANAGED_BLOCK_START}\n{block.rstrip()}\n{MANAGED_BLOCK_END}\n"
    pattern = re.compile(
        re.escape(MANAGED_BLOCK_START) + r".*?" + re.escape(MANAGED_BLOCK_END) + r"\n?",
        re.DOTALL,
    )
    existing_without_managed = pattern.sub("", existing)
    if LEGACY_LOADER_PATTERN.search(existing_without_managed):
        updated = append_managed_block(strip_legacy_loader_text(existing_without_managed), managed)
    elif pattern.search(existing):
        updated = pattern.sub(managed, existing)
    else:
        updated = append_managed_block(existing, managed)
    if updated != existing:
        path.write_text(updated, encoding="utf-8")
        record_action(actions, "write-loader-instructions", path=str(path))


def write_project_loader_instructions(project: Path, agents: list[str], actions: list[dict]) -> None:
    shared_line = "Project shared skills live in `.agents/skills`."
    for agent in agents:
        filename = PROJECT_AGENT_FILES.get(agent)
        if not filename:
            continue
        specific = f".{agent}/skills"
        block = "\n".join(
            [
                "# Project Skills",
                "",
                f"{shared_line} {agent.title()}-specific project skills live in `{specific}`.",
                "",
                f"When working in this repository, use `.agents/skills` for agent-neutral skills and `{specific}` only for {agent.title()}-specific behavior.",
                "",
                "Do not duplicate the same skill name in both directories. Keep one canonical copy.",
            ]
        )
        ensure_managed_block(project / filename, block, actions)


def migrate_legacy_project_skills(project: Path, shared_root: Path, actions: list[dict]) -> None:
    legacy = project / "skills"
    if not legacy.exists() and not legacy.is_symlink():
        return

    if legacy.is_symlink():
        try:
            target = legacy.resolve(strict=True)
        except FileNotFoundError:
            legacy.unlink()
            record_action(actions, "remove-broken-legacy-skills-symlink", path=str(legacy))
            return
        if shared_root.exists() and target == shared_root.resolve():
            legacy.unlink()
            record_action(actions, "remove-legacy-skills-symlink", path=str(legacy))
            return
        raise LayoutError(f"legacy skills symlink points outside shared root: {legacy} -> {target}")

    if shared_root.is_symlink() and shared_root.exists() and shared_root.resolve() == legacy.resolve():
        shared_root.unlink()
        shared_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(shared_root))
        record_action(actions, "move-legacy-skills", source=str(legacy), target=str(shared_root))
        return

    if not shared_root.exists():
        shared_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(shared_root))
        record_action(actions, "move-legacy-skills", source=str(legacy), target=str(shared_root))
        return

    if shared_root.resolve() == legacy.resolve():
        return

    conflicts = sorted({path.name for path in skill_dirs(shared_root)} & {path.name for path in skill_dirs(legacy)})
    if conflicts:
        raise LayoutError(f"legacy skills conflict with shared root: {', '.join(conflicts)}")
    for skill_dir in skill_dirs(legacy):
        shutil.move(str(skill_dir), str(shared_root / skill_dir.name))
        record_action(actions, "move-legacy-skill", source=str(skill_dir), target=str(shared_root / skill_dir.name))
    if not any(legacy.iterdir()):
        legacy.rmdir()
        record_action(actions, "remove-empty-legacy-skills-dir", path=str(legacy))
    else:
        raise LayoutError(f"legacy skills directory still contains non-skill files: {legacy}")


def ensure_agent_skill_dirs(project: Path, agents: list[str], actions: list[dict]) -> None:
    for agent in agents:
        if agent == "shared":
            continue
        root = project / f".{agent}" / "skills"
        if root.is_symlink():
            root.unlink()
            record_action(actions, "remove-agent-skills-symlink", agent=agent, path=str(root))
        root.mkdir(parents=True, exist_ok=True)
        record_action(actions, "ensure-agent-skills-dir", agent=agent, path=str(root))


def remove_identical_agent_duplicates(project: Path, agents: list[str], actions: list[dict]) -> None:
    shared_root = project / ".agents" / "skills"
    shared_by_name = {path.name: path for path in skill_dirs(shared_root)}
    for agent in agents:
        if agent == "shared":
            continue
        root = project / f".{agent}" / "skills"
        for skill_dir in skill_dirs(root):
            shared = shared_by_name.get(skill_dir.name)
            if shared is None:
                continue
            if same_skill_content(shared, skill_dir):
                shutil.rmtree(skill_dir)
                record_action(
                    actions,
                    "remove-identical-duplicate",
                    agent=agent,
                    skill=skill_dir.name,
                    path=str(skill_dir),
                    canonical=str(shared),
                )
            else:
                raise LayoutError(f"conflicting skill exists in shared and {agent}: {skill_dir.name}")


def apply_project_layout(
    project: Path,
    agents: list[str] | None = None,
    config_path: Path | None = None,
) -> list[dict]:
    project = project.expanduser().resolve()
    agents = validate_agents(agents if agents is not None else list_managed_agents(config_path))

    actions: list[dict] = []
    shared_root = project / ".agents" / "skills"
    migrate_legacy_project_skills(project, shared_root, actions)
    shared_root.mkdir(parents=True, exist_ok=True)
    record_action(actions, "ensure-shared-skills-dir", path=str(shared_root))
    ensure_agent_skill_dirs(project, agents, actions)
    remove_identical_agent_duplicates(project, agents, actions)
    write_project_loader_instructions(project, agents, actions)
    return actions


def ensure_native_bridge(source: Path, target_root: Path, agent: str, scope: str, actions: list[dict]) -> None:
    target_root.mkdir(parents=True, exist_ok=True)
    bridge = target_root / source.name
    canonical = source.resolve()

    if bridge.is_symlink():
        try:
            current = bridge.resolve(strict=True)
        except FileNotFoundError:
            bridge.unlink()
            record_action(actions, "remove-broken-native-bridge", agent=agent, scope=scope, path=str(bridge))
        else:
            if current == canonical:
                return
            raise LayoutError(f"native bridge points elsewhere: {bridge} -> {current}")

    if bridge.exists():
        if same_skill_content(source, bridge):
            if bridge.is_dir():
                shutil.rmtree(bridge)
            else:
                bridge.unlink()
            record_action(
                actions,
                "replace-identical-copy-with-native-bridge",
                agent=agent,
                scope=scope,
                path=str(bridge),
                canonical=str(source),
            )
        else:
            raise LayoutError(f"conflicting native skill already exists for {agent}: {bridge.name}")

    bridge.symlink_to(canonical, target_is_directory=True)
    record_action(
        actions,
        "create-native-bridge",
        agent=agent,
        scope=scope,
        path=str(bridge),
        canonical=str(source),
    )


def apply_native_bridges(
    home: Path | None = None,
    project: Path | None = None,
    agents: list[str] | None = None,
    config_path: Path | None = None,
    bridge_scope: str = "both",
) -> list[dict]:
    home = (home or Path.home()).expanduser().resolve()
    project = project.expanduser().resolve() if project is not None else None
    agents = validate_agents(agents if agents is not None else list_managed_agents(config_path))
    if bridge_scope not in BRIDGE_SCOPES:
        raise LayoutError(f"unknown bridge scope: {bridge_scope}")
    if bridge_scope == "project" and project is None:
        raise LayoutError("project bridge scope requires --project")

    actions: list[dict] = []
    scopes = []
    if bridge_scope in {"global", "both"}:
        scopes.append(("global", home))
    if bridge_scope in {"project", "both"} and project is not None:
        scopes.append(("project", project))

    for scope, base in scopes:
        shared_root = base / ".agents" / "skills"
        shared_root.mkdir(parents=True, exist_ok=True)
        record_action(actions, "ensure-shared-skills-dir", scope=scope, path=str(shared_root))
        shared_skills = skill_dirs(shared_root)
        for agent in agents:
            target_root = base / AGENT_DIRS[agent]
            target_root.mkdir(parents=True, exist_ok=True)
            record_action(actions, "ensure-agent-skills-dir", agent=agent, scope=scope, path=str(target_root))
            for source in shared_skills:
                ensure_native_bridge(source, target_root, agent, scope, actions)

    return actions


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def trash_root(home: Path) -> Path:
    return home.expanduser().resolve() / TRASH_ROOT_REL


def safe_trash_slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug or "skill"


def unique_trash_dir(home: Path, skill_name: str, now: datetime | None = None) -> Path:
    current = normalize_now(now)
    base = trash_root(home)
    stem = f"{current.strftime('%Y%m%dT%H%M%SZ')}-{safe_trash_slug(skill_name)}"
    candidate = base / stem
    index = 2
    while candidate.exists():
        candidate = base / f"{stem}-{index}"
        index += 1
    return candidate


def find_skill_matches(home: Path, project: Path | None, skill_name: str) -> list[dict]:
    skills, _ = discover_skills(home, project)
    return [skill for skill in skills if skill["name"] == skill_name or skill["folder"] == skill_name]


def remove_native_bridges_for_skill(
    home: Path,
    project: Path | None,
    skill_path: Path,
    skill_name: str,
    actions: list[dict],
) -> list[dict]:
    removed: list[dict] = []
    canonical = skill_path.resolve()
    bases = [("global", home)]
    if project is not None:
        bases.append(("project", project))

    for scope, base in bases:
        for agent, rel in AGENT_DIRS.items():
            if agent == "shared":
                continue
            bridge = base / rel / skill_path.name
            if not bridge.is_symlink() and skill_path.name != skill_name:
                bridge = base / rel / skill_name
            if not bridge.is_symlink():
                continue
            try:
                current = bridge.resolve(strict=True)
            except FileNotFoundError:
                current = None
            if current is not None and current != canonical:
                continue
            target = os.readlink(bridge)
            bridge.unlink()
            record = {
                "action": "remove-native-bridge",
                "agent": agent,
                "scope": scope,
                "path": str(bridge),
                "target": target,
            }
            actions.append(record)
            removed.append(record)
    return removed


def write_trash_manifest(trash_dir: Path, manifest: dict) -> None:
    (trash_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def quarantine_skills(
    home: Path | None = None,
    project: Path | None = None,
    skill_names: list[str] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    home = (home or Path.home()).expanduser().resolve()
    project = project.expanduser().resolve() if project is not None else None
    skill_names = skill_names or []
    actions: list[dict] = []

    for skill_name in skill_names:
        matches = find_skill_matches(home, project, skill_name)
        if not matches:
            actions.append({"action": "not-found", "skill": skill_name})
            continue

        for skill in matches:
            path = Path(skill["path"])
            if skill["protected"]:
                actions.append({"action": "skip-protected", "skill": skill["name"], "path": str(path)})
                continue
            if not path.exists() and not path.is_symlink():
                actions.append({"action": "not-found", "skill": skill["name"], "path": str(path)})
                continue

            trash_dir = unique_trash_dir(home, skill["name"], now)
            trash_dir.mkdir(parents=True)
            removed_bridges = remove_native_bridges_for_skill(home, project, path, skill["name"], actions)
            target = trash_dir / path.name
            shutil.move(str(path), str(target))
            manifest = {
                "created_at": normalize_now(now).isoformat(),
                "skill": skill["name"],
                "folder": skill["folder"],
                "scope": skill["scope"],
                "agent": skill["agent"],
                "original_path": str(path),
                "quarantined_path": str(target),
                "removed_bridges": removed_bridges,
            }
            write_trash_manifest(trash_dir, manifest)
            actions.append(
                {
                    "action": "quarantine-skill",
                    "skill": skill["name"],
                    "path": str(path),
                    "trash_dir": str(trash_dir),
                }
            )
    return actions


def delete_skills_permanently(
    home: Path | None = None,
    project: Path | None = None,
    skill_names: list[str] | None = None,
) -> list[dict]:
    home = (home or Path.home()).expanduser().resolve()
    project = project.expanduser().resolve() if project is not None else None
    skill_names = skill_names or []
    actions: list[dict] = []

    for skill_name in skill_names:
        matches = find_skill_matches(home, project, skill_name)
        if not matches:
            actions.append({"action": "not-found", "skill": skill_name})
            continue
        for skill in matches:
            path = Path(skill["path"])
            if skill["protected"]:
                actions.append({"action": "skip-protected", "skill": skill["name"], "path": str(path)})
                continue
            if not path.exists() and not path.is_symlink():
                actions.append({"action": "not-found", "skill": skill["name"], "path": str(path)})
                continue
            remove_native_bridges_for_skill(home, project, path, skill["name"], actions)
            remove_path(path)
            actions.append({"action": "delete-skill", "skill": skill["name"], "path": str(path)})
    return actions


def load_trash_manifests(home: Path | None = None) -> list[dict]:
    home = (home or Path.home()).expanduser().resolve()
    root = trash_root(home)
    manifests: list[dict] = []
    if not root.exists():
        return manifests
    for manifest_file in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        manifest["trash_dir"] = str(manifest_file.parent)
        manifest["status"] = "quarantined" if Path(manifest.get("quarantined_path", "")).exists() else "restored"
        manifests.append(manifest)
    return sorted(manifests, key=lambda item: (item.get("created_at", ""), item.get("skill", "")), reverse=True)


def select_trash_manifest(home: Path, selector: str) -> dict | None:
    manifests = load_trash_manifests(home)
    matches = [
        manifest
        for manifest in manifests
        if manifest.get("status") == "quarantined"
        and (manifest.get("skill") == selector or Path(manifest.get("trash_dir", "")).name == selector)
    ]
    return matches[0] if matches else None


def restore_skills(home: Path | None = None, selectors: list[str] | None = None) -> list[dict]:
    home = (home or Path.home()).expanduser().resolve()
    selectors = selectors or []
    actions: list[dict] = []

    for selector in selectors:
        manifest = select_trash_manifest(home, selector)
        if manifest is None:
            actions.append({"action": "trash-not-found", "selector": selector})
            continue
        source = Path(manifest["quarantined_path"])
        target = Path(manifest["original_path"])
        if target.exists() or target.is_symlink():
            actions.append({"action": "restore-conflict", "skill": manifest["skill"], "path": str(target)})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        actions.append({"action": "restore-skill", "skill": manifest["skill"], "path": str(target)})

        for bridge in manifest.get("removed_bridges", []):
            bridge_path = Path(bridge["path"])
            if bridge_path.exists() or bridge_path.is_symlink():
                actions.append({"action": "restore-bridge-conflict", "skill": manifest["skill"], "path": str(bridge_path)})
                continue
            bridge_path.parent.mkdir(parents=True, exist_ok=True)
            bridge_path.symlink_to(target.resolve(), target_is_directory=True)
            actions.append({"action": "restore-native-bridge", "skill": manifest["skill"], "path": str(bridge_path)})
    return actions


def default_log_roots(home: Path, project: Path | None = None) -> list[Path]:
    candidates = [
        home / ".codex" / "sessions",
        home / ".codex" / "history.jsonl",
        home / EVENT_LOG_REL,
        home / ".claude" / "projects",
        home / ".claude" / "sessions",
        home / ".gemini" / "history",
        home / ".cursor" / "projects",
    ]
    candidates.extend((home / ".gemini" / "tmp").glob("*/chats"))
    if project is not None:
        candidates.extend(
            [
                project / ".codex",
                project / ".claude",
                project / ".gemini",
                project / ".cursor",
            ]
        )
    return [path for path in candidates if path.exists()]


def walk_log_paths(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_LOG_DIRS and not name.startswith(".venv")]
        base = Path(dirpath)
        for filename in filenames:
            yield base / filename


def infer_agent_from_path(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    for agent in ("codex", "claude", "gemini", "cursor"):
        if f".{agent}" in parts or agent in parts:
            return agent
    return "unknown"


def line_has_term(line: str, terms: tuple[str, ...]) -> bool:
    lower = line.lower()
    return any(term in lower for term in terms)


def normalize_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_timestamp_value(value: object) -> float | None:
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return timestamp
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    try:
        return parse_timestamp_value(float(text))
    except ValueError:
        pass
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    if re.search(r"[+-][0-9]{4}$", text):
        text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def line_timestamp(line: str, fallback_timestamp: float) -> float:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for key in TIMESTAMP_KEYS:
            parsed = parse_timestamp_value(payload.get(key))
            if parsed is not None:
                return parsed

    match = ISO_TIMESTAMP_PATTERN.search(line)
    if match:
        parsed = parse_timestamp_value(match.group(0))
        if parsed is not None:
            return parsed
    return fallback_timestamp


def read_recent_log_text(path: Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > READ_TAIL_BYTES:
            handle.seek(-READ_TAIL_BYTES, os.SEEK_END)
        return handle.read().decode("utf-8", errors="ignore")


def skill_name_pattern(names: list[str]) -> tuple[dict[str, str], re.Pattern | None]:
    name_lookup = {name.lower(): name for name in names}
    if not names:
        return name_lookup, None
    alternates = "|".join(re.escape(name.lower()) for name in sorted(names, key=len, reverse=True))
    return name_lookup, re.compile(r"(?<![A-Za-z0-9_-])(" + alternates + r")(?![A-Za-z0-9_-])")


def matched_skill_names(line: str, name_lookup: dict[str, str], name_pattern: re.Pattern | None) -> set[str]:
    if name_pattern is None:
        return set()
    return {name_lookup[match.group(1)] for match in name_pattern.finditer(line.lower())}


def skill_name_boundary(name: str) -> str:
    return r"(?<![A-Za-z0-9_-])" + re.escape(name.lower()) + r"(?![A-Za-z0-9_-])"


def parse_json_line(line: str) -> dict | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def payload_skill_names(payload: dict) -> set[str]:
    names = set()
    for key in STRUCTURED_SKILL_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            names.add(value.lower())
        elif isinstance(value, list):
            names.update(item.lower() for item in value if isinstance(item, str))
    return names


def line_agent(line: str, fallback_agent: str) -> str:
    payload = parse_json_line(line)
    if isinstance(payload, dict) and isinstance(payload.get("agent"), str) and payload["agent"].strip():
        return payload["agent"].strip().lower()
    return fallback_agent


def structured_signal(line: str, skill_name: str) -> str | None:
    payload = parse_json_line(line)
    if payload is None or skill_name.lower() not in payload_skill_names(payload):
        return None
    events = {
        str(payload.get(key, "")).strip().lower().replace("-", "_")
        for key in STRUCTURED_EVENT_KEYS
        if payload.get(key) is not None
    }
    if events & USED_EVENTS:
        return "used"
    if events & LIKELY_EVENTS:
        return "likely"
    return None


def negates_skill_use(lower_line: str, skill_name: str) -> bool:
    name = skill_name_boundary(skill_name)
    patterns = [
        rf"\b(?:do not|don't|did not|didn't|not|without)\s+"
        rf"(?:use|using|load|loading|invoke|invoking|activate|activating|run|running)\s+"
        rf"(?:the\s+)?(?:skill\s+)?{name}",
        rf"(?:不要|别|没有|未)(?:使用|加载|调用|激活)\s*{name}",
    ]
    return any(re.search(pattern, lower_line) for pattern in patterns)


def classify_skill_signal(line: str, skill_name: str) -> str:
    structured = structured_signal(line, skill_name)
    if structured:
        return structured

    lower = line.lower()
    if negates_skill_use(lower, skill_name):
        return "mention"

    name = skill_name_boundary(skill_name)
    strong_patterns = [
        rf"\b(?:using|used|loaded|loading|invoked|invoking|activated|activating|selected|selecting|triggered|triggering)\s+"
        rf"(?:the\s+)?(?:skill\s+)?{name}",
        rf"(?:skill\s+)?{name}\s+(?:was\s+|is\s+|has\s+been\s+)?"
        rf"(?:used|loaded|invoked|activated|selected|triggered)",
        rf"(?:正在使用|已使用|加载了|调用了|激活了)\s*(?:skill\s*)?{name}",
    ]
    if any(re.search(pattern, lower) for pattern in strong_patterns):
        return "used"

    medium_patterns = [
        rf"\b(?:use|run|call|trigger|select|try|apply)\s+(?:the\s+)?(?:skill\s+)?{name}",
        rf"\b(?:ask|tell)\s+.*{name}",
        rf"(?:使用|用|运行|调用)\s*(?:skill\s*)?{name}",
    ]
    if any(re.search(pattern, lower) for pattern in medium_patterns):
        return "likely"

    return "mention"


def iter_log_files(roots: Iterable[Path], days: int, now: datetime | None = None) -> Iterable[Path]:
    cutoff = None
    current = normalize_now(now)
    if days > 0:
        cutoff = current.timestamp() - days * 86400

    for root in roots:
        if not root.exists():
            continue
        for path in walk_log_paths(root):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if cutoff is not None and stat.st_mtime < cutoff:
                continue
            if stat.st_size > MAX_LOG_BYTES:
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES and path.suffix:
                continue
            yield path


def usage_report(skills: list[dict], log_roots: list[Path], days: int, now: datetime | None = None) -> list[dict]:
    names = sorted({skill["name"] for skill in skills})
    counters = {
        name: {
            "name": name,
            "total_mentions": 0,
            "by_agent": {},
            "positive_signals": 0,
            "negative_signals": 0,
            "effectiveness_score": None,
            "last_seen": None,
        }
        for name in names
    }
    name_lookup, name_pattern = skill_name_pattern(names)

    for log_file in iter_log_files(log_roots, days, now):
        agent = infer_agent_from_path(log_file)
        try:
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime, timezone.utc).isoformat()
            lines = read_recent_log_text(log_file).splitlines()
        except OSError:
            continue

        for line in lines:
            lower = line.lower()
            matched_names = matched_skill_names(line, name_lookup, name_pattern)
            if not matched_names:
                continue
            event_agent = line_agent(line, agent)
            positive = line_has_term(lower, POSITIVE_TERMS)
            negative = line_has_term(lower, NEGATIVE_TERMS)
            for name in matched_names:
                row = counters[name]
                row["total_mentions"] += 1
                row["by_agent"][event_agent] = row["by_agent"].get(event_agent, 0) + 1
                if positive:
                    row["positive_signals"] += 1
                if negative:
                    row["negative_signals"] += 1
                if row["last_seen"] is None or mtime > row["last_seen"]:
                    row["last_seen"] = mtime

    for row in counters.values():
        signals = row["positive_signals"] + row["negative_signals"]
        if signals:
            row["effectiveness_score"] = round(row["positive_signals"] / signals, 2)

    return sorted(
        counters.values(),
        key=lambda item: (item["total_mentions"], item["last_seen"] or ""),
        reverse=True,
    )


def usage_window_report(
    skills: list[dict],
    log_roots: list[Path],
    now: datetime | None = None,
    windows: tuple[tuple[str, str, int], ...] = USAGE_WINDOWS,
) -> list[dict]:
    current = normalize_now(now)
    current_timestamp = current.timestamp()
    max_days = max(days for _, _, days in windows)
    names = sorted({skill["name"] for skill in skills})
    counters = {
        name: {
            "name": name,
            **{key: 0 for key, _, _ in windows},
            "by_agent": {},
            "last_seen": None,
        }
        for name in names
    }
    name_lookup, name_pattern = skill_name_pattern(names)

    for log_file in iter_log_files(log_roots, max_days, current):
        agent = infer_agent_from_path(log_file)
        try:
            fallback_timestamp = log_file.stat().st_mtime
            lines = read_recent_log_text(log_file).splitlines()
        except OSError:
            continue

        for line in lines:
            matched_names = matched_skill_names(line, name_lookup, name_pattern)
            if not matched_names:
                continue
            event_agent = line_agent(line, agent)
            event_timestamp = line_timestamp(line, fallback_timestamp)
            age_seconds = current_timestamp - event_timestamp
            if age_seconds > max_days * 86400:
                continue
            event_iso = datetime.fromtimestamp(event_timestamp, timezone.utc).isoformat()
            for name in matched_names:
                row = counters[name]
                by_agent = row["by_agent"].setdefault(event_agent, {key: 0 for key, _, _ in windows})
                for key, _, days in windows:
                    if age_seconds <= days * 86400:
                        row[key] += 1
                        by_agent[key] += 1
                if row["last_seen"] is None or event_iso > row["last_seen"]:
                    row["last_seen"] = event_iso

    return sorted(
        counters.values(),
        key=lambda item: tuple([-item[key] for key, _, _ in reversed(windows)]) + (item["name"],),
    )


def usage_confidence_report(
    skills: list[dict],
    log_roots: list[Path],
    days: int,
    now: datetime | None = None,
) -> list[dict]:
    current = normalize_now(now)
    names = sorted({skill["name"] for skill in skills})
    counters = {
        name: {
            "name": name,
            "mentions": 0,
            "actual_or_likely_uses": 0,
            "strong_signals": 0,
            "medium_signals": 0,
            "weak_signals": 0,
            "success_signals": 0,
            "failure_signals": 0,
            "confidence": 0.0,
            "event_type": "none",
            "by_agent": {},
            "last_seen": None,
        }
        for name in names
    }
    name_lookup, name_pattern = skill_name_pattern(names)

    for log_file in iter_log_files(log_roots, days, current):
        agent = infer_agent_from_path(log_file)
        try:
            fallback_timestamp = log_file.stat().st_mtime
            lines = read_recent_log_text(log_file).splitlines()
        except OSError:
            continue

        for line in lines:
            matched_names = matched_skill_names(line, name_lookup, name_pattern)
            if not matched_names:
                continue
            event_agent = line_agent(line, agent)
            lower = line.lower()
            positive = line_has_term(lower, POSITIVE_TERMS)
            negative = line_has_term(lower, NEGATIVE_TERMS)
            event_timestamp = line_timestamp(line, fallback_timestamp)
            event_iso = datetime.fromtimestamp(event_timestamp, timezone.utc).isoformat()

            for name in matched_names:
                signal = classify_skill_signal(line, name)
                row = counters[name]
                row["mentions"] += 1
                if signal in {"used", "likely"}:
                    row["actual_or_likely_uses"] += 1
                if signal == "used":
                    row["strong_signals"] += 1
                elif signal == "likely":
                    row["medium_signals"] += 1
                else:
                    row["weak_signals"] += 1
                if positive:
                    row["success_signals"] += 1
                if negative:
                    row["failure_signals"] += 1
                if row["last_seen"] is None or event_iso > row["last_seen"]:
                    row["last_seen"] = event_iso

                agent_row = row["by_agent"].setdefault(
                    event_agent,
                    {
                        "mentions": 0,
                        "actual_or_likely_uses": 0,
                        "strong_signals": 0,
                        "medium_signals": 0,
                        "weak_signals": 0,
                        "success_signals": 0,
                        "failure_signals": 0,
                    },
                )
                agent_row["mentions"] += 1
                if signal in {"used", "likely"}:
                    agent_row["actual_or_likely_uses"] += 1
                if signal == "used":
                    agent_row["strong_signals"] += 1
                elif signal == "likely":
                    agent_row["medium_signals"] += 1
                else:
                    agent_row["weak_signals"] += 1
                if positive:
                    agent_row["success_signals"] += 1
                if negative:
                    agent_row["failure_signals"] += 1

    for row in counters.values():
        mentions = row["mentions"]
        if mentions:
            weighted = (
                row["strong_signals"] * SIGNAL_WEIGHTS["used"]
                + row["medium_signals"] * SIGNAL_WEIGHTS["likely"]
                + row["weak_signals"] * SIGNAL_WEIGHTS["mention"]
            )
            row["confidence"] = round(weighted / mentions, 2)
        if row["strong_signals"]:
            row["event_type"] = "used"
        elif row["medium_signals"]:
            row["event_type"] = "likely"
        elif row["weak_signals"]:
            row["event_type"] = "mention"

    return sorted(
        counters.values(),
        key=lambda item: (
            -item["actual_or_likely_uses"],
            -item["confidence"],
            -item["mentions"],
            item["name"],
        ),
    )


def cleanup_recommendation_report(
    skills: list[dict],
    usage_confidence: list[dict],
    now: datetime | None = None,
    stale_days: int = 30,
) -> list[dict]:
    current = normalize_now(now)
    usage_by_name = {item["name"]: item for item in usage_confidence}
    rows: list[dict] = []

    for skill in skills:
        usage = usage_by_name.get(skill["name"], {})
        last_seen = usage.get("last_seen")
        last_seen_days_ago = None
        if last_seen:
            parsed = parse_timestamp_value(last_seen)
            if parsed is not None:
                last_seen_days_ago = round((current.timestamp() - parsed) / 86400, 1)

        base = {
            "skill": skill["name"],
            "path": skill["path"],
            "scope": skill["scope"],
            "agent": skill["agent"],
            "protected": skill["protected"],
            "mentions": usage.get("mentions", 0),
            "actual_or_likely_uses": usage.get("actual_or_likely_uses", 0),
            "confidence": usage.get("confidence", 0.0),
            "event_type": usage.get("event_type", "none"),
            "last_seen": last_seen,
            "last_seen_days_ago": last_seen_days_ago,
        }

        if skill["protected"]:
            recommendation = "protected-or-skip"
            reason = "Protected runtime or bundled skill."
        elif base["mentions"] == 0:
            recommendation = "review-manually"
            reason = "No usage evidence found in scanned logs."
        elif (
            base["actual_or_likely_uses"] == 0
            and base["confidence"] <= 0.25
            and last_seen_days_ago is not None
            and last_seen_days_ago >= stale_days
        ):
            recommendation = "safe-to-remove"
            reason = f"Only weak mentions found and last seen at least {stale_days} days ago."
        elif base["actual_or_likely_uses"] == 0 or base["confidence"] < 0.4:
            recommendation = "review-manually"
            reason = "Low-confidence usage evidence."
        else:
            recommendation = "keep"
            reason = "Recent or credible usage evidence found."

        base["recommendation"] = recommendation
        base["reason"] = reason
        rows.append(base)

    order = {"safe-to-remove": 0, "review-manually": 1, "keep": 2, "protected-or-skip": 3}
    return sorted(
        rows,
        key=lambda item: (
            order[item["recommendation"]],
            -float(item["last_seen_days_ago"] or -1),
            item["skill"],
            item["path"],
        ),
    )


def add_quality_issue(issues: list[dict], code: str, severity: str, detail: str, penalty: int, **fields: object) -> None:
    issue = {"code": code, "severity": severity, "detail": detail, "penalty": penalty}
    issue.update(fields)
    issues.append(issue)


def policy_string_set(policy: dict, key: str) -> set[str]:
    value = policy.get(key, [])
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def quality_issue_is_ignored(issue: dict, skill: dict, policy: dict) -> bool:
    ignored_codes = policy_string_set(policy, "ignore_issue_codes")
    ignored_skills = policy_string_set(policy, "ignore_skills")
    ignored_paths = policy_string_set(policy, "ignore_paths")
    if issue["code"] in ignored_codes:
        return True
    if skill["name"] in ignored_skills or skill["folder"] in ignored_skills:
        return True
    return any(pattern and pattern in skill["path"] for pattern in ignored_paths)


def description_is_broad(description: str) -> bool:
    value = description.strip().lower()
    if not value:
        return False
    if len(value) < 24:
        return True
    return any(term in value for term in BROAD_DESCRIPTION_TERMS)


def non_executable_shebang_scripts(skill_dir: Path) -> list[str]:
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return []

    scripts: list[str] = []
    for path in sorted(scripts_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                first_line = handle.readline(128)
        except OSError:
            continue
        if first_line.startswith(b"#!") and not os.access(path, os.X_OK):
            scripts.append(str(path.relative_to(skill_dir)))
    return scripts


def skill_quality_report(skills: list[dict], policy: dict | None = None) -> list[dict]:
    policy = policy or {}
    rows: list[dict] = []
    for skill in skills:
        issues: list[dict] = []
        path = Path(skill["path"])
        description = skill.get("description", "")
        skill_file = path / "SKILL.md"
        try:
            skill_text = skill_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skill_text = ""

        if not description.strip():
            add_quality_issue(
                issues,
                "missing-description",
                "high",
                "SKILL.md frontmatter should include a trigger-focused description.",
                30,
            )
        elif description_is_broad(description):
            add_quality_issue(
                issues,
                "broad-description",
                "medium",
                "Description is too broad to help agents decide when to load the skill.",
                20,
            )

        if HARDCODED_ABSOLUTE_PATH_PATTERN.search(skill_text):
            add_quality_issue(
                issues,
                "hardcoded-absolute-path",
                "medium",
                "Skill text contains a machine-specific absolute path; prefer $HOME, relative paths, or placeholders.",
                15,
            )

        scripts = non_executable_shebang_scripts(path)
        if scripts:
            add_quality_issue(
                issues,
                "non-executable-script",
                "medium",
                f"Shebang script is not executable: {', '.join(scripts[:5])}.",
                15,
                paths=scripts,
                fix="chmod-executable",
            )

        metadata_agent_mentions = set(skill.get("metadata_agent_mentions", []))
        content_agent_mentions = set(skill.get("content_agent_mentions", []))
        desired_agent = skill.get("desired_agent")
        if skill["agent"] == "shared" and desired_agent is not None:
            add_quality_issue(
                issues,
                "agent-specific-in-shared",
                "medium",
                "Shared skill metadata appears to target one agent; consider moving it to that agent's skill directory.",
                15,
            )
        elif (
            skill["agent"] != "shared"
            and desired_agent is None
            and skill["agent"] not in metadata_agent_mentions
            and skill["agent"] not in content_agent_mentions
        ):
            add_quality_issue(
                issues,
                "agent-neutral-in-agent-specific",
                "low",
                "Agent-specific skill does not advertise agent-specific behavior; consider moving it to shared skills.",
                10,
            )

        active_issues: list[dict] = []
        ignored_issues: list[dict] = []
        for issue in issues:
            if quality_issue_is_ignored(issue, skill, policy):
                ignored_issues.append(issue)
            else:
                active_issues.append(issue)
        score = max(0, 100 - sum(issue["penalty"] for issue in active_issues))
        rows.append(
            {
                "skill": skill["name"],
                "path": skill["path"],
                "scope": skill["scope"],
                "agent": skill["agent"],
                "protected": skill["protected"],
                "quality_score": score,
                "issues": active_issues,
                "ignored_issues": ignored_issues,
            }
        )

    return sorted(rows, key=lambda item: (item["quality_score"], item["skill"], item["path"]))


def fix_quality_issues(quality: list[dict]) -> list[dict]:
    actions: list[dict] = []
    for item in quality:
        for issue in item.get("issues", []):
            if issue.get("code") != "non-executable-script":
                continue
            if item.get("protected"):
                actions.append(
                    {
                        "action": "skip-protected-fix",
                        "skill": item["skill"],
                        "issue": issue["code"],
                        "path": item["path"],
                    }
                )
                continue

            for rel_path in issue.get("paths", []):
                script = Path(item["path"]) / rel_path
                if script.is_symlink():
                    actions.append(
                        {
                            "action": "skip-symlink-script",
                            "skill": item["skill"],
                            "path": str(script),
                        }
                    )
                    continue
                if not script.is_file():
                    actions.append(
                        {
                            "action": "script-not-found",
                            "skill": item["skill"],
                            "path": str(script),
                        }
                    )
                    continue
                try:
                    mode_before = script.stat().st_mode
                    mode_after = mode_before | 0o111
                    if mode_after != mode_before:
                        script.chmod(mode_after)
                except OSError as exc:
                    actions.append(
                        {
                            "action": "chmod-failed",
                            "skill": item["skill"],
                            "path": str(script),
                            "error": str(exc),
                        }
                    )
                    continue
                actions.append(
                    {
                        "action": "chmod-executable",
                        "skill": item["skill"],
                        "path": str(script),
                        "mode_before": oct(mode_before & 0o777),
                        "mode_after": oct(mode_after & 0o777),
                    }
                )
    return actions


def build_recommendations(skills: list[dict], duplicates: list[dict], usage: list[dict], days: int) -> list[dict]:
    recommendations: list[dict] = []
    usage_by_name = {item["name"]: item for item in usage}

    for duplicate in duplicates:
        unprotected = [loc for loc in duplicate["locations"] if not loc["protected"]]
        if len(unprotected) < 2:
            continue
        recommendations.append(
            {
                "skill": duplicate["name"],
                "recommendation": "deduplicate",
                "reason": "Same skill name exists in multiple roots.",
                "locations": [loc["path"] for loc in duplicate["locations"]],
            }
        )

    for skill in skills:
        if skill["protected"]:
            continue
        metadata_agent_mentions = set(skill.get("metadata_agent_mentions", []))
        content_agent_mentions = set(skill.get("content_agent_mentions", []))
        desired_agent = skill["desired_agent"]
        if skill["agent"] == "shared" and desired_agent is not None:
            recommendations.append(
                {
                    "skill": skill["name"],
                    "recommendation": "move-to-agent-specific",
                    "target_agent": desired_agent,
                    "reason": "Shared skill appears to mention one agent or vendor specifically.",
                    "path": skill["path"],
                }
            )
        elif (
            skill["agent"] != "shared"
            and desired_agent is None
            and skill["agent"] not in metadata_agent_mentions
            and skill["agent"] not in content_agent_mentions
        ):
            target = "project shared" if skill["scope"] == "project" else "global shared"
            recommendations.append(
                {
                    "skill": skill["name"],
                    "recommendation": "move-to-shared",
                    "target": target,
                    "reason": "Agent-specific skill does not advertise agent-specific behavior.",
                    "path": skill["path"],
                }
            )

    for skill in skills:
        if skill["protected"]:
            continue
        used = usage_by_name.get(skill["name"], {})
        if used.get("total_mentions", 0) == 0:
            recommendations.append(
                {
                    "skill": skill["name"],
                    "recommendation": "review-or-prune",
                    "reason": f"No usage mentions found in scanned logs during the last {days} days.",
                    "path": skill["path"],
                }
            )
        elif used.get("negative_signals", 0) > used.get("positive_signals", 0):
            recommendations.append(
                {
                    "skill": skill["name"],
                    "recommendation": "review-effectiveness",
                    "reason": "Negative nearby signals exceed positive nearby signals in scanned logs.",
                    "path": skill["path"],
                }
            )

    unique: dict[tuple, dict] = {}
    for item in recommendations:
        key = (item["skill"], item["recommendation"], item.get("path", ""))
        unique[key] = item
    return sorted(unique.values(), key=lambda item: (item["skill"], item["recommendation"], item.get("path", "")))


def build_report(
    home: Path | None = None,
    project: Path | None = None,
    days: int = 90,
    log_roots: list[Path] | None = None,
    now: datetime | None = None,
    stale_days: int = 30,
    policy_path: Path | None = None,
) -> dict:
    current = normalize_now(now)
    home = (home or Path.home()).expanduser().resolve()
    project = project.expanduser().resolve() if project is not None else None
    policy, loaded_policy_path = load_policy(project, policy_path)
    quality_policy = policy.get("quality", {})
    if not isinstance(quality_policy, dict):
        quality_policy = {}
    skills, roots = discover_skills(home, project)
    duplicates = duplicate_report(skills)
    logs = default_log_roots(home, project) if log_roots is None else [path.expanduser().resolve() for path in log_roots]
    usage = usage_report(skills, logs, days, current)
    usage_windows = usage_window_report(skills, logs, current)
    usage_confidence = usage_confidence_report(skills, logs, days, current)
    cleanup_recommendations = cleanup_recommendation_report(skills, usage_confidence, current, stale_days)
    quality = skill_quality_report(skills, quality_policy)
    recommendations = build_recommendations(skills, duplicates, usage, days)
    return {
        "generated_at": current.isoformat(),
        "home": str(home),
        "project": str(project) if project else None,
        "policy_path": str(loaded_policy_path) if loaded_policy_path else None,
        "days": days,
        "log_roots": [str(path) for path in logs],
        "roots": roots,
        "skills": skills,
        "duplicates": duplicates,
        "usage": usage,
        "usage_windows": usage_windows,
        "usage_confidence": usage_confidence,
        "cleanup_recommendations": cleanup_recommendations,
        "quality": quality,
        "recommendations": recommendations,
    }


def print_text(report: dict) -> None:
    print("Agent Skill Management Report")
    print(f"Home: {report['home']}")
    if report["project"]:
        print(f"Project: {report['project']}")
    if report.get("policy_path"):
        print(f"Policy: {report['policy_path']}")
    print(f"Skills: {len(report['skills'])}")
    print(f"Duplicates: {len(report['duplicates'])}")
    print(f"Quality Issues: {sum(1 for item in report.get('quality', []) if item.get('issues'))}")
    print(f"Recommendations: {len(report['recommendations'])}")
    print()

    if report.get("layout_actions"):
        print("Layout Actions")
        for item in report["layout_actions"]:
            details = " ".join(f"{key}={value}" for key, value in item.items() if key != "action")
            print(f"- {item['action']}{(' ' + details) if details else ''}")
        print()

    if report["duplicates"]:
        print("Duplicates")
        for item in report["duplicates"][:20]:
            print(f"- {item['name']} ({len(item['locations'])} locations, same_content={item['same_content']})")
            for loc in item["locations"]:
                marker = " protected" if loc["protected"] else ""
                print(f"  - {loc['scope']}:{loc['agent']} {loc['path']}{marker}")
        print()

    if report["usage"]:
        print("Usage Ranking")
        for item in report["usage"][:20]:
            score = item["effectiveness_score"]
            score_text = "n/a" if score is None else f"{score:.2f}"
            print(
                f"- {item['name']}: mentions={item['total_mentions']} "
                f"score={score_text} last_seen={item['last_seen'] or 'never'}"
            )
        print()

    if report.get("usage_windows"):
        print("Usage by Window")
        rows = report["usage_windows"]
        name_width = max([len("Skill"), *[len(item["name"]) for item in rows]])
        headers = " ".join(label.rjust(5) for _, label, _ in USAGE_WINDOWS)
        print(f"{'Skill':<{name_width}} {headers} Last Seen")
        print(f"{'-' * name_width} {'-' * len(headers)} ---------")
        for item in rows:
            counts = " ".join(str(item[key]).rjust(5) for key, _, _ in USAGE_WINDOWS)
            print(f"{item['name']:<{name_width}} {counts} {item['last_seen'] or 'never'}")
        print()

    if report.get("usage_confidence"):
        print("Usage Confidence")
        rows = report["usage_confidence"]
        name_width = max([len("Skill"), *[len(item["name"]) for item in rows]])
        print(f"{'Skill':<{name_width}} {'Likely':>6} {'Mentions':>8} {'Confidence':>10} {'Type':>7} {'+':>3} {'-':>3} Last Seen")
        print(f"{'-' * name_width} {'-' * 6} {'-' * 8} {'-' * 10} {'-' * 7} {'-' * 3} {'-' * 3} ---------")
        for item in rows:
            print(
                f"{item['name']:<{name_width}} "
                f"{item['actual_or_likely_uses']:>6} "
                f"{item['mentions']:>8} "
                f"{item['confidence']:>10.2f} "
                f"{item['event_type']:>7} "
                f"{item['success_signals']:>3} "
                f"{item['failure_signals']:>3} "
                f"{item['last_seen'] or 'never'}"
            )
        print()

    quality_issues = [item for item in report.get("quality", []) if item["issues"]]
    if quality_issues:
        print("Skill Quality")
        name_width = max([len("Skill"), *[len(item["skill"]) for item in quality_issues]])
        print(f"{'Skill':<{name_width}} {'Score':>5} Issues")
        print(f"{'-' * name_width} {'-' * 5} ------")
        for item in quality_issues[:40]:
            codes = ", ".join(issue["code"] for issue in item["issues"])
            print(f"{item['skill']:<{name_width}} {item['quality_score']:>5} {codes}")
            print(f"  path: {item['path']}")
        print()

    if report.get("cleanup_recommendations"):
        print("Cleanup Recommendations")
        for group in ("safe-to-remove", "review-manually", "keep", "protected-or-skip"):
            items = [item for item in report["cleanup_recommendations"] if item["recommendation"] == group]
            if not items:
                continue
            print(f"{group}: {len(items)}")
            for item in items[:20]:
                print(
                    f"- {item['skill']}: uses={item['actual_or_likely_uses']} "
                    f"mentions={item['mentions']} confidence={item['confidence']:.2f} "
                    f"last_seen={item['last_seen'] or 'never'}"
                )
                print(f"  reason: {item['reason']}")
        print()

    if report["recommendations"]:
        print("Recommendations")
        for item in report["recommendations"][:40]:
            print(f"- {item['skill']}: {item['recommendation']} - {item['reason']}")
            if "path" in item:
                print(f"  path: {item['path']}")


def html_cell(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def html_table(headers: list[str], rows: list[list[object]]) -> str:
    head = "".join(f"<th>{html_cell(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{html_cell(cell)}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")
    body = "\n".join(body_rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html_report(report: dict) -> str:
    quality_rows = [
        [
            item["skill"],
            item["quality_score"],
            ", ".join(issue["code"] for issue in item["issues"]) or "ok",
            item["path"],
        ]
        for item in report.get("quality", [])
    ]
    cleanup_rows = [
        [
            item["skill"],
            item["recommendation"],
            item["actual_or_likely_uses"],
            item["mentions"],
            f"{item['confidence']:.2f}",
            item["last_seen"] or "never",
            item["reason"],
        ]
        for item in report.get("cleanup_recommendations", [])
    ]
    window_rows = [
        [
            item["name"],
            item["last_24h"],
            item["last_7d"],
            item["last_30d"],
            item["last_seen"] or "never",
        ]
        for item in report.get("usage_windows", [])
    ]
    confidence_rows = [
        [
            item["name"],
            item["actual_or_likely_uses"],
            item["mentions"],
            f"{item['confidence']:.2f}",
            item["event_type"],
            item["success_signals"],
            item["failure_signals"],
            item["last_seen"] or "never",
        ]
        for item in report.get("usage_confidence", [])
    ]
    duplicate_rows = [
        [
            item["name"],
            len(item["locations"]),
            item["same_content"],
            "; ".join(loc["path"] for loc in item["locations"]),
        ]
        for item in report.get("duplicates", [])
    ]

    styles = """
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:32px;color:#1f2937;background:#f8fafc}
h1{font-size:28px;margin:0 0 8px}
h2{font-size:18px;margin:28px 0 10px}
.meta,.summary{color:#4b5563}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:12px}
.value{font-size:24px;font-weight:700;color:#111827}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:left;font-size:13px;vertical-align:top}
th{background:#f3f4f6;font-weight:600}
tr:last-child td{border-bottom:0}
code{background:#eef2ff;padding:2px 4px;border-radius:4px}
"""
    sections = [
        "<!doctype html>",
        "<html><head><meta charset=\"utf-8\"><title>Skill Steward Report</title>",
        f"<style>{styles}</style></head><body>",
        "<h1>Skill Steward Report</h1>",
        f"<div class=\"meta\">Generated at {html_cell(report.get('generated_at'))}</div>",
        f"<div class=\"meta\">Home: <code>{html_cell(report.get('home'))}</code></div>",
    ]
    if report.get("project"):
        sections.append(f"<div class=\"meta\">Project: <code>{html_cell(report.get('project'))}</code></div>")
    if report.get("policy_path"):
        sections.append(f"<div class=\"meta\">Policy: <code>{html_cell(report.get('policy_path'))}</code></div>")
    sections.extend(
        [
            "<div class=\"cards\">",
            f"<div class=\"card\"><div>Skills</div><div class=\"value\">{len(report.get('skills', []))}</div></div>",
            f"<div class=\"card\"><div>Duplicates</div><div class=\"value\">{len(report.get('duplicates', []))}</div></div>",
            f"<div class=\"card\"><div>Quality Issues</div><div class=\"value\">{sum(1 for item in report.get('quality', []) if item.get('issues'))}</div></div>",
            f"<div class=\"card\"><div>Cleanup Items</div><div class=\"value\">{len(report.get('cleanup_recommendations', []))}</div></div>",
            "</div>",
            "<h2>Skill Quality</h2>",
            html_table(["Skill", "Score", "Issues", "Path"], quality_rows),
            "<h2>Cleanup Recommendations</h2>",
            html_table(["Skill", "Recommendation", "Likely Uses", "Mentions", "Confidence", "Last Seen", "Reason"], cleanup_rows),
            "<h2>Usage by Window</h2>",
            html_table(["Skill", "24h", "7d", "30d", "Last Seen"], window_rows),
            "<h2>Usage Confidence</h2>",
            html_table(["Skill", "Likely Uses", "Mentions", "Confidence", "Type", "+", "-", "Last Seen"], confidence_rows),
            "<h2>Duplicates</h2>",
            html_table(["Skill", "Locations", "Same Content", "Paths"], duplicate_rows),
            "</body></html>",
        ]
    )
    return "\n".join(sections)


def print_actions(actions: list[dict]) -> None:
    for action in actions:
        details = " ".join(f"{key}={value}" for key, value in action.items() if key != "action")
        print(f"- {action['action']}{(' ' + details) if details else ''}")


def handle_skills_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Quarantine, restore, or delete skill directories safely.")
    parser.add_argument("--home", type=Path, default=Path.home(), help="Home directory.")
    parser.add_argument("--project", type=Path, help="Optional project directory.")
    parser.add_argument("--policy", type=Path, help="Optional skill-steward policy JSON file.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    parser.add_argument("--stale-days", type=int, default=30, help="Age threshold for safe-to-remove suggestions.")
    subparsers = parser.add_subparsers(dest="skills_command", required=True)

    def add_common_options(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--home", type=Path, default=argparse.SUPPRESS, help="Home directory.")
        subparser.add_argument("--project", type=Path, default=argparse.SUPPRESS, help="Optional project directory.")
        subparser.add_argument("--policy", type=Path, default=argparse.SUPPRESS, help="Optional skill-steward policy JSON file.")
        subparser.add_argument("--format", choices=("text", "json"), default=argparse.SUPPRESS, help="Output format.")
        subparser.add_argument("--stale-days", type=int, default=argparse.SUPPRESS, help="Age threshold for safe-to-remove suggestions.")

    quarantine_parser = subparsers.add_parser("quarantine", help="Move skills to the local skill-steward trash.")
    quarantine_parser.add_argument("skills", nargs="+", help="Skill names or folder names to quarantine.")
    add_common_options(quarantine_parser)

    delete_parser = subparsers.add_parser("delete", help="Permanently delete skills. Requires --yes.")
    delete_parser.add_argument("skills", nargs="+", help="Skill names or folder names to delete.")
    delete_parser.add_argument("--yes", action="store_true", help="Confirm permanent deletion.")
    add_common_options(delete_parser)

    restore_parser = subparsers.add_parser("restore", help="Restore quarantined skills by skill name or trash id.")
    restore_parser.add_argument("selectors", nargs="+", help="Skill names or trash directory ids to restore.")
    add_common_options(restore_parser)

    list_trash_parser = subparsers.add_parser("list-trash", help="List quarantined skills.")
    add_common_options(list_trash_parser)

    cleanup_parser = subparsers.add_parser("cleanup-plan", help="Suggest skills to remove, review, keep, or skip.")
    cleanup_parser.add_argument("--days", type=int, default=90, help="Log scan window in days.")
    cleanup_parser.add_argument("--log-root", action="append", type=Path, help="Additional or explicit log root to scan.")
    add_common_options(cleanup_parser)

    quality_parser = subparsers.add_parser("quality", help="Check skill metadata, placement, paths, and helper scripts.")
    quality_parser.add_argument("--all", action="store_true", help="Show skills without quality issues in text output.")
    quality_parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply safe automatic fixes, currently chmod +x for non-protected shebang scripts.",
    )
    add_common_options(quality_parser)

    args = parser.parse_args(argv)
    try:
        if args.skills_command == "quarantine":
            actions = quarantine_skills(home=args.home, project=args.project, skill_names=args.skills)
            payload = {"actions": actions}
        elif args.skills_command == "delete":
            if not args.yes:
                raise LayoutError("permanent delete requires --yes; use skills quarantine for reversible cleanup")
            actions = delete_skills_permanently(home=args.home, project=args.project, skill_names=args.skills)
            payload = {"actions": actions}
        elif args.skills_command == "restore":
            actions = restore_skills(home=args.home, selectors=args.selectors)
            payload = {"actions": actions}
        elif args.skills_command == "list-trash":
            payload = {"trash": load_trash_manifests(args.home)}
        elif args.skills_command == "cleanup-plan":
            report = build_report(
                home=args.home,
                project=args.project,
                days=args.days,
                log_roots=args.log_root,
                stale_days=args.stale_days,
                policy_path=args.policy,
            )
            payload = {
                "cleanup_recommendations": report["cleanup_recommendations"],
                "generated_at": report["generated_at"],
                "home": report["home"],
                "project": report["project"],
                "policy_path": report["policy_path"],
            }
        elif args.skills_command == "quality":
            fix_actions: list[dict] = []
            report = build_report(
                home=args.home,
                project=args.project,
                log_roots=[],
                stale_days=args.stale_days,
                policy_path=args.policy,
            )
            if args.fix:
                fix_actions = fix_quality_issues(report["quality"])
                report = build_report(
                    home=args.home,
                    project=args.project,
                    log_roots=[],
                    stale_days=args.stale_days,
                    policy_path=args.policy,
                )
            payload = {
                "quality": report["quality"],
                "fix_actions": fix_actions,
                "generated_at": report["generated_at"],
                "home": report["home"],
                "project": report["project"],
                "policy_path": report["policy_path"],
            }
        else:
            parser.error(f"unknown command: {args.skills_command}")
    except LayoutError as exc:
        print(f"Skill command failed: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        print()
    elif "actions" in payload:
        print_actions(payload["actions"])
    elif "cleanup_recommendations" in payload:
        print("Cleanup Plan")
        for item in payload["cleanup_recommendations"]:
            print(f"- {item['skill']}: {item['recommendation']} - {item['reason']}")
    elif "quality" in payload:
        print("Skill Quality")
        rows = payload["quality"] if getattr(args, "all", False) else [item for item in payload["quality"] if item["issues"]]
        if not rows:
            print("- no quality issues found")
        for item in rows:
            codes = ", ".join(issue["code"] for issue in item["issues"]) or "ok"
            print(f"- {item['skill']}: score={item['quality_score']} issues={codes}")
        if getattr(args, "fix", False):
            print("Fix Actions")
            if not payload["fix_actions"]:
                print("- no fixable issues found")
            else:
                print_actions(payload["fix_actions"])
    else:
        for item in payload["trash"]:
            print(
                f"- {item.get('skill')} status={item.get('status')} "
                f"trash={Path(item.get('trash_dir', '')).name} original={item.get('original_path')}"
            )
    return 0


def event_log_path(home: Path | None = None) -> Path:
    home = (home or Path.home()).expanduser().resolve()
    return home / EVENT_LOG_REL


def record_usage_event(
    event: str,
    skill: str,
    agent: str = "unknown",
    outcome: str = "unknown",
    home: Path | None = None,
    project: Path | None = None,
    now: datetime | None = None,
) -> dict:
    path = event_log_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": normalize_now(now).isoformat(),
        "event": event,
        "skill": skill,
        "agent": agent,
        "outcome": outcome,
        "text": f"skill-steward event {event} {skill} {outcome}",
    }
    if project is not None:
        payload["project"] = str(project.expanduser().resolve())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return {"action": "record-event", "path": str(path), "event": payload}


def handle_event_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Record an explicit skill usage event.")
    parser.add_argument("event", choices=("used", "likely", "mention"), help="Event strength to record.")
    parser.add_argument("skill", help="Skill name.")
    parser.add_argument("--agent", default="unknown", help="Agent name, for example codex or claude.")
    parser.add_argument("--outcome", choices=("success", "failure", "unknown"), default="unknown", help="Outcome signal.")
    parser.add_argument("--home", type=Path, default=Path.home(), help="Home directory.")
    parser.add_argument("--project", type=Path, help="Optional project directory.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    args = parser.parse_args(argv)

    record = record_usage_event(
        event=args.event,
        skill=args.skill,
        agent=args.agent,
        outcome=args.outcome,
        home=args.home,
        project=args.project,
    )
    if args.format == "json":
        json.dump(record, sys.stdout, indent=2, sort_keys=True)
        print()
    else:
        print(f"Recorded {record['event']['event']} for {record['event']['skill']} at {record['path']}")
    return 0


def handle_agents_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manage skill-steward agent configuration.")
    parser.add_argument("--config", type=Path, help="Config file path. Defaults to ~/.config/skill-steward/config.json.")
    subparsers = parser.add_subparsers(dest="agent_command", required=True)

    list_parser = subparsers.add_parser("list", help="List managed agents.")
    list_parser.add_argument("--config", type=Path, help=argparse.SUPPRESS)

    for command in ("add", "delete", "set"):
        sub = subparsers.add_parser(command, help=f"{command.title()} managed agents.")
        sub.add_argument(
            "agents",
            nargs="*",
            help="Optional automation shortcut. Omit to choose from a numbered list.",
        )
        sub.add_argument("--config", type=Path, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    config_path = args.config
    try:
        if args.agent_command == "list":
            managed = list_managed_agents(config_path)
        elif args.agent_command == "add":
            current = list_managed_agents(config_path)
            if args.agents:
                selected = args.agents
            else:
                available = [agent for agent in AGENT_CHOICES if agent not in current]
                selected = choose_agents("Select agents to add:", available, default=[], allow_empty=True)
            managed = add_managed_agents(selected, config_path)
        elif args.agent_command == "delete":
            current = list_managed_agents(config_path)
            selected = args.agents or choose_agents("Select agents to delete:", current, default=[], allow_empty=True)
            managed = delete_managed_agents(selected, config_path)
        elif args.agent_command == "set":
            current = list_managed_agents(config_path)
            selected = args.agents or choose_agents(
                "Select managed agents:",
                AGENT_CHOICES,
                default=current,
                allow_empty=True,
            )
            managed = set_managed_agents(selected, config_path)
        else:
            parser.error(f"unknown command: {args.agent_command}")
    except LayoutError as exc:
        print(f"Agent config failed: {exc}", file=sys.stderr)
        return 1
    print("\n".join(managed))
    return 0


def handle_install_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Initialize skill-steward managed agent configuration.")
    parser.add_argument(
        "agents",
        nargs="*",
        help="Optional automation shortcut. Omit to choose from a numbered list.",
    )
    parser.add_argument("--home", type=Path, default=Path.home(), help="Home directory for global skill bridges.")
    parser.add_argument("--config", type=Path, help="Config file path. Defaults to ~/.config/skill-steward/config.json.")
    parser.add_argument("--project", type=Path, help="Optionally apply the canonical project layout after writing config.")
    parser.add_argument(
        "--native-bridges",
        action="store_true",
        help="Create symlink bridges from shared skills into each selected agent's native skills directory.",
    )
    parser.add_argument(
        "--bridge-scope",
        choices=BRIDGE_SCOPES,
        default="both",
        help="Scope for --native-bridges. Defaults to both when --project is supplied.",
    )
    args = parser.parse_args(argv)

    try:
        selected = args.agents or choose_agents(
            "Select agents for skill-steward to manage:",
            AGENT_CHOICES,
            default=DEFAULT_PROJECT_AGENTS,
            allow_empty=False,
        )
        managed = set_managed_agents(selected, args.config)
        actions = apply_project_layout(args.project, agents=managed, config_path=args.config) if args.project else []
        bridge_actions = (
            apply_native_bridges(
                args.home,
                project=args.project,
                agents=managed,
                config_path=args.config,
                bridge_scope=args.bridge_scope,
            )
            if args.native_bridges
            else []
        )
    except LayoutError as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return 1

    print("Managed agents:")
    print("\n".join(managed))
    if actions:
        print("Layout actions:")
        for action in actions:
            details = " ".join(f"{key}={value}" for key, value in action.items() if key != "action")
            print(f"- {action['action']}{(' ' + details) if details else ''}")
    if bridge_actions:
        print("Native bridge actions:")
        for action in bridge_actions:
            details = " ".join(f"{key}={value}" for key, value in action.items() if key != "action")
            print(f"- {action['action']}{(' ' + details) if details else ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "event":
        return handle_event_command(argv[1:])
    if argv and argv[0] == "skills":
        return handle_skills_command(argv[1:])
    if argv and argv[0] == "agents":
        return handle_agents_command(argv[1:])
    if argv and argv[0] in {"list", "add", "delete", "set"}:
        return handle_agents_command(argv)
    if argv and argv[0] == "install":
        return handle_install_command(argv[1:])

    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Config commands: install, list, set, add, delete. "
            "They present numbered agent choices; positional agent names are automation shortcuts. "
            "Explicit agents list/add/delete/set subcommands are also supported."
        ),
    )
    parser.add_argument("--home", type=Path, default=Path.home(), help="Home directory to scan.")
    parser.add_argument("--project", type=Path, help="Project directory to scan for project-level skills.")
    parser.add_argument("--days", type=int, default=90, help="Only scan logs modified in the last N days; 0 scans all.")
    parser.add_argument("--stale-days", type=int, default=30, help="Age threshold for safe-to-remove cleanup suggestions.")
    parser.add_argument("--log-root", action="append", type=Path, help="Additional or explicit log root to scan.")
    parser.add_argument("--config", type=Path, help="Config file path for managed agents.")
    parser.add_argument("--policy", type=Path, help="Optional skill-steward policy JSON file.")
    parser.add_argument(
        "--apply-project-layout",
        action="store_true",
        help="Create canonical project skill directories, migrate legacy ./skills, and remove identical agent-specific duplicates.",
    )
    parser.add_argument(
        "--apply-native-bridges",
        action="store_true",
        help="Symlink shared skills into managed agents' native skills directories without copying canonical skill contents.",
    )
    parser.add_argument(
        "--bridge-scope",
        choices=BRIDGE_SCOPES,
        default="both",
        help="Scope for --apply-native-bridges: global, project, or both.",
    )
    parser.add_argument(
        "--agent",
        action="append",
        help="Agent-specific project skill directory to ensure when using --apply-project-layout. Repeatable; overrides configured managed agents.",
    )
    parser.add_argument("--format", choices=("text", "json", "html"), default="text", help="Output format.")
    args = parser.parse_args(argv)

    layout_actions: list[dict] = []
    if args.apply_project_layout:
        if args.project is None:
            parser.error("--apply-project-layout requires --project")
        try:
            layout_actions = apply_project_layout(args.project, agents=args.agent, config_path=args.config)
        except LayoutError as exc:
            print(f"Project layout failed: {exc}", file=sys.stderr)
            return 1
    if args.apply_native_bridges:
        try:
            layout_actions.extend(
                apply_native_bridges(
                    args.home,
                    project=args.project,
                    agents=args.agent,
                    config_path=args.config,
                    bridge_scope=args.bridge_scope,
                )
            )
        except LayoutError as exc:
            print(f"Native bridge setup failed: {exc}", file=sys.stderr)
            return 1

    log_roots = args.log_root if args.log_root else None
    try:
        report = build_report(
            home=args.home,
            project=args.project,
            days=args.days,
            log_roots=log_roots,
            stale_days=args.stale_days,
            policy_path=args.policy,
        )
    except LayoutError as exc:
        print(f"Report failed: {exc}", file=sys.stderr)
        return 1
    if layout_actions:
        report["layout_actions"] = layout_actions
    if args.format == "json":
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        print()
    elif args.format == "html":
        print(render_html_report(report))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
