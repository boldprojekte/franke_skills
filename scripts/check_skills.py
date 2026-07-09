#!/usr/bin/env python3
"""check_skills: validate that every skills/**/SKILL.md has a name and description."""

from __future__ import annotations

import sys
from pathlib import Path

REQUIRED_FIELDS = ("name", "description")
BLOCK_SCALAR_INDICATORS = {"|", ">", "|-", ">-", "|+", ">+"}


def strip_quotes(value: str) -> str:
    """Drop a single pair of matching surrounding quotes from a scalar value."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Parse the leading ``---`` fenced YAML frontmatter into top-level scalar fields.

    Returns a mapping of top-level key to its (quote-stripped) value, or ``None`` when
    the file has no terminated frontmatter block. Block scalars and wrapped values are
    collapsed to their joined indented content so emptiness can be judged.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    end: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None

    body = lines[1:end]
    fields: dict[str, str] = {}
    i = 0
    while i < len(body):
        line = body[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or line[0].isspace() or ":" not in line:
            i += 1
            continue

        key, _, rest = line.partition(":")
        key = key.strip()
        value = rest.strip()
        if value == "" or value in BLOCK_SCALAR_INDICATORS:
            # Empty inline value: gather any indented / continuation lines that follow.
            collected: list[str] = []
            j = i + 1
            while j < len(body) and (body[j].strip() == "" or body[j][:1].isspace()):
                if body[j].strip():
                    collected.append(body[j].strip())
                j += 1
            fields[key] = strip_quotes(" ".join(collected))
            i = j
            continue

        fields[key] = strip_quotes(value)
        i += 1

    return fields


def check_skill_file(path: Path) -> list[str]:
    """Return a list of human-readable problems for a single SKILL.md (empty if valid)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [f"could not read file ({exc})"]

    fields = parse_frontmatter(text)
    if fields is None:
        return ["missing YAML frontmatter"]

    problems: list[str] = []
    for key in REQUIRED_FIELDS:
        value = fields.get(key)
        if value is None:
            problems.append(f"missing '{key}'")
        elif not value.strip():
            problems.append(f"empty '{key}'")
    return problems


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo_root = Path(__file__).resolve().parent.parent
    skills_dir = Path(argv[0]) if argv else repo_root / "skills"

    if not skills_dir.is_dir():
        print(f"error: skills directory not found: {skills_dir}", file=sys.stderr)
        return 1

    skill_files = sorted(skills_dir.rglob("SKILL.md"))
    violations = 0
    for path in skill_files:
        try:
            display: Path | str = path.relative_to(repo_root)
        except ValueError:
            display = path
        for problem in check_skill_file(path):
            print(f"{display}: {problem}")
            violations += 1

    if violations:
        return 1

    print(f"OK {len(skill_files)} skills checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
