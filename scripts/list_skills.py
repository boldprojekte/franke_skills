#!/usr/bin/env python3
"""list_skills: catalog every skills/**/SKILL.md as JSON or a markdown table."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
FIELD_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")


def unquote(value: str) -> str:
    """Strip a single matching pair of surrounding quotes, YAML-style."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict[str, str]:
    """Return the `key: value` pairs from a leading `---` fenced YAML block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = FIELD_RE.match(line)
        if match:
            fields[match.group(1)] = unquote(match.group(2))
    return fields


def collect_skills(skills_dir: Path) -> list[dict[str, str]]:
    """Scan skills_dir for SKILL.md files and pull name/description/category."""
    skills: list[dict[str, str]] = []
    for skill_file in sorted(skills_dir.rglob("SKILL.md")):
        parts = skill_file.relative_to(skills_dir).parts
        category = parts[0] if len(parts) > 1 else ""
        fields = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        skills.append(
            {
                "category": category,
                "name": fields.get("name", ""),
                "description": fields.get("description", ""),
            }
        )
    skills.sort(key=lambda s: (s["category"], s["name"]))
    return skills


def render_markdown(skills: list[dict[str, str]]) -> str:
    """Render the skills as a GitHub-flavored markdown table."""

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    rows = ["| Category | Name | Description |", "| --- | --- | --- |"]
    rows += [
        f"| {cell(s['category'])} | {cell(s['name'])} | {cell(s['description'])} |"
        for s in skills
    ]
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List every skills/**/SKILL.md as JSON (default) or a markdown table.",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="print a markdown table instead of a JSON array",
    )
    args = parser.parse_args(argv)

    skills = collect_skills(SKILLS_DIR) if SKILLS_DIR.is_dir() else []

    if args.markdown:
        print(render_markdown(skills))
    else:
        print(json.dumps(skills, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
