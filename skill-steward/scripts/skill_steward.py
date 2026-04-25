#!/usr/bin/env python3
"""Inventory and usage reporting for shared and agent-specific skills."""

from __future__ import annotations

import argparse
import hashlib
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
LIKELY_EVENTS = {"request", "requested", "recommend", "recommended", "suggest", "suggested", "select", "selected"}
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
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key in {"name", "description"}:
            values[key] = value
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


def default_log_roots(home: Path, project: Path | None = None) -> list[Path]:
    candidates = [
        home / ".codex" / "sessions",
        home / ".codex" / "history.jsonl",
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
            positive = line_has_term(lower, POSITIVE_TERMS)
            negative = line_has_term(lower, NEGATIVE_TERMS)
            for name in matched_names:
                row = counters[name]
                row["total_mentions"] += 1
                row["by_agent"][agent] = row["by_agent"].get(agent, 0) + 1
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
            event_timestamp = line_timestamp(line, fallback_timestamp)
            age_seconds = current_timestamp - event_timestamp
            if age_seconds > max_days * 86400:
                continue
            event_iso = datetime.fromtimestamp(event_timestamp, timezone.utc).isoformat()
            for name in matched_names:
                row = counters[name]
                by_agent = row["by_agent"].setdefault(agent, {key: 0 for key, _, _ in windows})
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
                    agent,
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
) -> dict:
    current = normalize_now(now)
    home = (home or Path.home()).expanduser().resolve()
    project = project.expanduser().resolve() if project is not None else None
    skills, roots = discover_skills(home, project)
    duplicates = duplicate_report(skills)
    logs = default_log_roots(home, project) if log_roots is None else [path.expanduser().resolve() for path in log_roots]
    usage = usage_report(skills, logs, days, current)
    usage_windows = usage_window_report(skills, logs, current)
    usage_confidence = usage_confidence_report(skills, logs, days, current)
    recommendations = build_recommendations(skills, duplicates, usage, days)
    return {
        "generated_at": current.isoformat(),
        "home": str(home),
        "project": str(project) if project else None,
        "days": days,
        "log_roots": [str(path) for path in logs],
        "roots": roots,
        "skills": skills,
        "duplicates": duplicates,
        "usage": usage,
        "usage_windows": usage_windows,
        "usage_confidence": usage_confidence,
        "recommendations": recommendations,
    }


def print_text(report: dict) -> None:
    print("Agent Skill Management Report")
    print(f"Home: {report['home']}")
    if report["project"]:
        print(f"Project: {report['project']}")
    print(f"Skills: {len(report['skills'])}")
    print(f"Duplicates: {len(report['duplicates'])}")
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

    if report["recommendations"]:
        print("Recommendations")
        for item in report["recommendations"][:40]:
            print(f"- {item['skill']}: {item['recommendation']} - {item['reason']}")
            if "path" in item:
                print(f"  path: {item['path']}")


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
    parser.add_argument("--log-root", action="append", type=Path, help="Additional or explicit log root to scan.")
    parser.add_argument("--config", type=Path, help="Config file path for managed agents.")
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
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
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
    report = build_report(home=args.home, project=args.project, days=args.days, log_roots=log_roots)
    if layout_actions:
        report["layout_actions"] = layout_actions
    if args.format == "json":
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        print()
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
