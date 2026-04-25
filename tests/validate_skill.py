#!/usr/bin/env python3
"""Small CI validator for a SKILL.md package."""

from __future__ import annotations

import re
import sys
from pathlib import Path


NAME_RE = re.compile(r"^[a-z0-9-]+$")


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md frontmatter must be closed with ---")
    data: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip("\"'")
    return data


def validate(skill_dir: Path) -> None:
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        raise ValueError(f"missing {skill_file}")

    metadata = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    name = metadata.get("name", "")
    description = metadata.get("description", "")

    if not name:
        raise ValueError("frontmatter must include name")
    if not NAME_RE.match(name):
        raise ValueError("name must contain only lowercase letters, digits, and hyphens")
    if name != skill_dir.name:
        raise ValueError(f"name {name!r} must match directory {skill_dir.name!r}")
    if not description:
        raise ValueError("frontmatter must include description")
    if len(description) > 1024:
        raise ValueError("description must be 1024 characters or fewer")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: validate_skill.py <skill-dir>", file=sys.stderr)
        return 2
    try:
        validate(Path(argv[1]))
    except ValueError as exc:
        print(f"Skill validation failed: {exc}", file=sys.stderr)
        return 1
    print("Skill is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
