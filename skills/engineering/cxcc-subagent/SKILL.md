---
name: cxcc-subagent
description: "Spawns, monitors, steers, and collects parallel Codex CLI or Claude Code coding tasks via the cdx supervisor. Use whenever implementation work is delegated to subagents — building from a frozen spec, refactors, mechanical migrations, bug fixes, test writing — and the session should keep working instead of blocking on codex exec. Also triggers for: checking whether a delegated run is still alive, answering a question a subagent escalated, redirecting or killing a runaway run, or fanning out several coding tasks in parallel."
---

# CXCC Subagent

`cdx` supervises detached `codex exec` sessions. Spawn returns immediately, a watchdog reaps true hangs, and escalated questions surface as an explicit state instead of dying in a log. Division of labour stays as in codex-first: Codex types, you think — spec before, review after.

The CLI is `scripts/cdx.py` inside this skill folder (in this repo: `skills/engineering/cxcc-subagent/scripts/cdx.py`); it runs from any cwd and needs only Python 3.10+.

```bash
CDX="skills/engineering/cxcc-subagent/scripts/cdx.py"        # then: python3 $CDX <verb> ...
ROLES="skills/engineering/cxcc-subagent/references/roles"
```

Every verb takes `--json` — use it always; stdout is pure JSON, diagnostics go to stderr. Exact schemas and exit codes: references/spec.md (read only when a field or code is genuinely unclear).

## Roles

A role is a prompt block that frames what kind of agent the task is; `-f` is repeatable and concatenates in order, so compose role first, task second:

```bash
python3 $CDX spawn -f $ROLES/general.md -f task.md -C <repo> --json
```

Pass role files by path — never read them; they are Codex-facing and cost you nothing. All you need is each role's interface: what your task file must contain.

| Role file | Use for | Your task file must contain |
|---|---|---|
| `roles/general.md` | implementation, refactors, bug fixes, tests — the default for hands-on work | a work order: goal, repo paths, constraints, non-goals, proof expected, output shape |
| `roles/review-standards.md` | reviewing a change against repo conventions + smell baseline | the review target file per references/review.md |
| `roles/review-spec.md` | reviewing a change against the plan/spec it was built from | the review target file per references/review.md |

**For any code review, read references/review.md first** — it defines the review contract (target, axes, sources — stated to the user before spawning), the target-file format, the parallel two-axis run, and the adjudication step. Don't improvise a review flow when that file exists.

## The loop

1. **Spawn.** Write the prompt as a work order — Codex has zero session context: goal, repo + key paths, constraints ("don't touch X"), non-goals, proof expected (exact test command), output shape. Then:
   ```bash
   python3 $CDX spawn -f prompt.md -C /path/to/repo --json    # returns {task, pid, state} instantly
   ```
   Done when: JSON came back with a task name. Do not wait here — move on.

2. **Work on something else.** The task runs detached and survives anything short of a reboot.

3. **Check in at natural pauses** — not on a poll loop:
   ```bash
   python3 $CDX list --json    # attention-first: awaiting_reply / failed / stalled sort to the top
   ```
   Done when: every task is accounted for — `working` tasks left alone, everything else acted on (below).

4. **Collect and verify.**
   ```bash
   python3 $CDX result <task> --json    # exit 0 done · 11 awaiting_reply · 13 failed · 10 still working
   ```
   For long tasks, run `result <task> --wait --json` as a background Bash and get notified instead of polling.
   A result is not an outcome: `git status -sb` + read the full diff in the repo, judge it like a contributor PR, run the proof command yourself. Codex claims are advisory. Done when: the diff is reviewed and the proof ran in your own shell.

## Acting on states

| State | Meaning | Your move |
|---|---|---|
| `working` | bytes still flowing | leave it alone |
| `awaiting_reply` | Codex escalated a `QUESTION:` | read it in `status`/`result`, answer via `send` |
| `done` | turn finished cleanly | collect, review, verify |
| `failed` | turn errored or process died | `peek` for the tail, then `send` a fix or respawn |
| `stalled` | watchdog killed a hang (no output for 5 min) | `send "continue"` resumes exactly where it stopped |
| `killed` | you killed it | resumable via `send` |

Answering and steering share one verb — same thread, full context retained:

```bash
python3 $CDX send <task> "Use the existing Zod schema in packages/config" --json   # answer / follow-up
python3 $CDX send <task> --now "Stop — wrong approach. Refactor X instead" --json  # interrupt a running task first
```

`send` refuses while a task is running; that refusal is the guard against accidental interrupts — reach for `--now` deliberately, when the thinking stream shows a wrong turn, not because you're impatient.

## When unsure what a task is doing

Escalate probes in cost order, and stop at the first one that settles wait-vs-steer-vs-kill:

```bash
python3 $CDX status <task> --json            # cheap: state, last-output age, last activity line
python3 $CDX peek <task> --json              # summarized recent events (commands, file changes, messages)
python3 $CDX peek <task> --thinking --json   # emergency only: ~1000 chars of live reasoning stream
```

`--thinking` is raw model stream and pays its tokens — never poll it. One look, decide, act.

## Fan-out

Parallel tasks are the point: separate repos (or non-overlapping dirs), one spawn each, one `list` to watch them all. Two tasks writing the same checkout will trample each other — cdx warns on spawn; take the warning seriously.

## Backends, models, effort

`spawn --backend codex|claude` (default codex) — identical verbs, states, and roles either way; pick claude when the task benefits from a Claude model, codex otherwise. `--effort medium|high|max` (default high) is a per-task choice — medium for mechanical work, max for the hardest problems; cdx translates to each backend's own scale. Model defaults are machine-level policy, not per-spawn knowledge: they live in `cdx config` (self-describing via `--help`) — touch it only when the user asks to change models; `--model` overrides a single task.

## Housekeeping

`python3 $CDX doctor` before first use of a session if anything smells off (binary, state dir, orphans). `python3 $CDX clean --terminal` once results are harvested — a lean task list keeps `list` readable.
