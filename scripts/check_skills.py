#!/usr/bin/env python3
"""check_skills: validate that every SKILL.md declares a name and description.

Walks the repository ``skills/`` tree and parses the leading YAML frontmatter of
each ``SKILL.md``. The parser is stdlib-only (no PyYAML dependency); it only
understands the simple ``key: value`` scalars that SKILL.md frontmatter uses.
Every file is checked for a non-empty ``name`` and a non-empty ``description``.

Output and exit codes:
    * one line per offending file: ``<path>: <what is missing>``
    * exit 1 when any violation is found, else exit 0 after printing
      ``OK <count> skills checked``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REQUIRED_FIELDS = ("name", "description")


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Return the top-level scalar keys of a leading ``---`` YAML block.

    Only unindented ``key: value`` lines are read; blanks, comments, and nested
    (indented) entries are ignored. Returns ``None`` when the file has no
    frontmatter block, i.e. it lacks either the opening or the closing ``---``
    fence.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict[str, str] = {}
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Ignore anything nested under a mapping/sequence; we only want the
        # top-level scalar fields.
        if line[:1] in (" ", "\t"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if not closed:
        return None
    return fields


def strip_quotes(value: str) -> str:
    """Drop a single matching pair of surrounding single or double quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def check_skill(path: Path) -> list[str]:
    """Return a list of problems for one SKILL.md (empty list means valid)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"unreadable: {exc}"]
    fields = parse_frontmatter(text)
    if fields is None:
        return ["missing YAML frontmatter"]
    problems: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in fields:
            problems.append(f"missing {field}")
        elif not strip_quotes(fields[field]).strip():
            problems.append(f"empty {field}")
    return problems


def find_skill_files(skills_dir: Path) -> list[Path]:
    """Return every SKILL.md under ``skills_dir``, sorted for stable output."""
    if not skills_dir.is_dir():
        return []
    return sorted(skills_dir.rglob("SKILL.md"))


def display_path(path: Path) -> str:
    """Path relative to the current directory when possible, else absolute."""
    try:
        return os.path.relpath(path)
    except ValueError:  # e.g. a different drive on Windows
        return str(path)


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=(
            "Validate that every SKILL.md has a non-empty name and description."
        ),
    )
    parser.add_argument(
        "skills_dir",
        nargs="?",
        default=repo_root / "skills",
        type=Path,
        help="directory to walk for SKILL.md files (default: <repo>/skills)",
    )
    args = parser.parse_args(argv)

    skill_files = find_skill_files(args.skills_dir)
    violations = 0
    for path in skill_files:
        problems = check_skill(path)
        if problems:
            violations += 1
            print(f"{display_path(path)}: {', '.join(problems)}")

    if violations:
        return 1
    print(f"OK {len(skill_files)} skills checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
