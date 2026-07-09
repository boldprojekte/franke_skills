# Two-axis review via cdx

Standards (repo conventions + smell baseline) and Spec (implementation vs the plan it was built from) run as two parallel cdx tasks. The axes stay separate end to end. A change can pass one and fail the other, and merging would let one mask the other. The reviewer prompts live in the role files; this file is only the procedure.

## 1. State the review contract: before spawning anything

Tell the user, in a few lines:

- **Target**, the exact scope: `git diff <fixed-point>...HEAD` (three-dot), a directory, a PR, or "what task `<name>` just built".
- **Axes**: Standards always runs. Spec runs only when a spec source exists.
- **Sources**: the standards documents found in the repo (CONTRIBUTING, coding-standards docs, AGENTS/CLAUDE.md sections); the spec/plan documents (markdown plans under `docs/`, a PRD section, a task's own work order at `~/.codex-agents/tasks/<name>/prompt.md`, or a source the user named). Spec sources outside the filesystem (e.g. an issue) get their content pasted into the target file below.

If the user is present and the scope is ambiguous, confirm before spending review tokens. Done when: the contract is stated and no correction came back.

## 2. Validate the target

`git rev-parse <fixed-point>` resolves and the diff is non-empty. A bad ref or empty diff fails here, not inside two parallel tasks. Then write one target file (scratchpad) shared by both axes:

```markdown
# Review target
Diff: git diff <fixed-point>...HEAD          # or explicit paths
Commits: <output of git log <fixed-point>..HEAD --oneline>
Standards documents: <paths, or "none, baseline only">
Spec sources: <paths or pasted content>      # omit section if no spec axis
```

Done when: ref resolved, diff non-empty, target file written.

## 3. Spawn both axes in parallel

```bash
python3 $CDX spawn -f $ROLES/review-standards.md -f target.md -C <repo> --name rev-std-<slug> --json
python3 $CDX spawn -f $ROLES/review-spec.md      -f target.md -C <repo> --name rev-spec-<slug> --json
```

Reviewers are read-only by role instruction; they read the standards/spec files themselves: paths suffice, no pasting repo content. Skip the spec spawn when there is no spec source, and say so in the final report.

## 4. Collect

`result --wait --json` for each (background Bash for long diffs). A valid report ends with a `FINDINGS:` line. If it's missing, `send` once asking for the report in the specified format.

## 5. Adjudicate: never skipped

The reviewers supply findings; the verdict is yours. Read each finding against the actual diff and classify it: real / not real / ship-blocking. Then report:

- `## Standards` and `## Spec`: the findings with your per-finding verdicts. Keep the axes separate; no merged ranking, no single winner across axes.
- One closing line per axis: finding count and the worst confirmed issue, if any.

Done when: every finding carries a verdict and the user has the per-axis summary.
