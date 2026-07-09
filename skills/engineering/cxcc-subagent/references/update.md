# Updating this skill

Procedure for updating an installed copy of `cxcc-subagent` to the latest published
version. Run it when the user asks to update the skill. Source of truth:
`https://github.com/boldprojekte/franke_skills` (branch `main`).

## 1. Locate the installed copy

The installed copy is this skill's own base directory (the folder containing this
references/ directory), typically `.claude/skills/cxcc-subagent` or
`.agents/skills/cxcc-subagent`.

**Source-checkout case:** if that directory sits inside a git clone of
`franke_skills` itself (check: `git -C <dir> remote get-url origin` mentions
`franke_skills`), the update is just `git pull` in that clone. Report the pulled
commits and stop; the steps below are for copied installs only.

## 2. Fetch the latest version

```bash
TMP=$(mktemp -d)
git clone --depth 1 https://github.com/boldprojekte/franke_skills.git "$TMP/repo"
SOURCE_DIR=$(find "$TMP/repo/skills" -path '*/cxcc-subagent' -type d | head -n 1)
```

Done when: the clone exists and `$SOURCE_DIR` is the fetched `cxcc-subagent` skill directory.

## 3. Diff before touching anything

```bash
diff -r -x __pycache__ <installed-dir> "$SOURCE_DIR"
```

- **No differences:** tell the user the skill is already up to date, clean up `$TMP`, stop.
- **Differences:** summarize them for the user in terms of behavior, not filenames.
  `$TMP/repo/CHANGELOG.md` (top entries) is the primary source for "what changed";
  the diff is the ground truth for what will be overwritten.

If the installed copy has local modifications the diff cannot distinguish from
upstream changes, say so explicitly before overwriting.

## 4. Check for running tasks, then apply

An update overwrites `scripts/cdx.py` in place. Already-running helpers keep their
loaded code, but avoid mixing versions mid-flight:

```bash
python3 <installed-dir>/scripts/cdx.py list --json   # any non-terminal task → ask the user before proceeding
```

Apply by replacing the installed copy with the fetched one (preserve the directory
path itself, replace its contents), then clean up `$TMP`.

## 5. Verify and report

```bash
python3 <installed-dir>/scripts/cdx.py doctor --json
```

Done when: doctor runs without errors. Report to the user: the version change
(old → new behavior, from the CHANGELOG entries), anything overwritten that had
local modifications, and that doctor passes.
