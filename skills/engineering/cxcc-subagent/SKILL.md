---
name: cxcc-subagent
description: "Spawns, monitors, steers, and collects detached Codex CLI, Claude Code, or Grok coding subagents via the cdx supervisor. Use whenever work is delegated to a subagent (implementation from a frozen spec, refactors, migrations, bug fixes, test writing, read-only codebase exploration, frontend/UI builds, E2E verification of a running app, or two-axis code review) and the session should keep working instead of blocking. Also triggers for: checking whether a delegated run is still alive, answering a question a subagent escalated, redirecting or killing a runaway run, fanning out parallel tasks, or updating this skill."
---

# CXCC Subagent

`cdx` supervises detached `codex exec` sessions. Spawn returns immediately, a watchdog reaps true hangs, and escalated questions surface as an explicit state instead of dying in a log. Division of labour stays as in codex-first: Codex types, you think. Spec before, review after.

The CLI is `scripts/cdx.py` inside this skill folder; it runs from any cwd and needs only Python 3.10+.

```bash
SKILL_DIR=<this skill's base directory, announced when the skill loads>
CDX="$SKILL_DIR/scripts/cdx.py"
ROLES="$SKILL_DIR/references/roles"
```

Every verb takes `--json`. Use it always; stdout is pure JSON, diagnostics go to stderr. Exit codes: 0 ok · 2 usage · 3 not found · 4 invalid state · 5 backend/internal · 6 timeout · 7 binary missing · 10 working · 11 awaiting_reply · 12 stalled · 13 failed/killed. If a JSON field is genuinely unclear, the source of truth is `scripts/cdx.py`.

## Roles

A role is a prompt block that frames what kind of agent the task is; `-f` is repeatable and concatenates in order, so compose role first, task second:

```bash
python3 $CDX spawn -f $ROLES/general.md -f task.md -C <repo> --json
```

Pass role files by path. Never read them; they are Codex-facing and cost you nothing. All you need is each role's interface: what your task file must contain.

| Role file | Use for | Your task file must contain |
|---|---|---|
| `roles/general.md` | implementation, refactors, bug fixes, tests: the default for hands-on work | a work order: goal, repo paths, constraints, non-goals, proof expected, output shape |
| `roles/explore.md` | read-only codebase questions: locating code, mapping how something works, checking whether X exists | the question(s), repo scope/paths, a thoroughness level (quick / medium / very thorough), any answer-format needs |
| `roles/frontend.md` | UI work: building new interfaces or changing existing ones without producing design slop | the brief/change, greenfield or brownfield, the pages/components that define the surrounding design (brownfield), brand constraints if any, proof expected (build + visual check) |
| `roles/computer-use.md` | E2E verification by driving the running product: browser flows, app behavior, screenshots, runtime state; user-triggered, not automatic | what was built/changed, the exact flows to drive, how to launch the app, expected behavior per flow, environment bounds (test accounts, what's off-limits) |
| `roles/review-standards.md` | reviewing a change against repo conventions + smell baseline | the review target file per references/review.md |
| `roles/review-spec.md` | reviewing a change against the plan/spec it was built from | the review target file per references/review.md |

**Explore tasks:** delegate only questions that would cost you more than a few directed searches; ask them specific and well-scoped, fan out parallel explorers for independent questions, and follow up on the same task via `send`. Trust the ANSWER/EVIDENCE/GAPS report. Don't re-run its searches.

**For any code review, read references/review.md first.** It defines the review contract (target, axes, and sources, all stated to the user before spawning), the target-file format, the parallel two-axis run, and the adjudication step. Don't improvise a review flow when that file exists.

## The loop

1. **Spawn.** Write the prompt as a work order. The worker has zero session context, so the work order carries exactly the **delta**: everything the worker needs that is NOT in the codebase. Decisions the user made in this session, constraints you learned, approaches already ruled out, verified facts the worker cannot rediscover. What IS in the codebase gets referenced by path, not repeated as text. And don't write the code in the prompt; you are delegating the typing, not dictating keystrokes. Structure: goal, repo + key paths, constraints ("don't touch X"), non-goals, proof expected (exact test command), output shape. Then:
   ```bash
   python3 $CDX spawn -f prompt.md -C /path/to/repo --json    # returns {task, pid, state} instantly
   ```
   Done when: JSON came back with a task name. Do not wait here; move on.

2. **Work on something else.** The task runs detached and survives anything short of a reboot.

3. **Check in at natural pauses**, not on a poll loop:
   ```bash
   python3 $CDX list --json    # attention-first: awaiting_reply / failed / stalled sort to the top
   ```
   Done when: every task is accounted for: `working` tasks left alone, everything else acted on (below).

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

Answering and steering share one verb (same thread, full context retained):

```bash
python3 $CDX send <task> "Use the existing Zod schema in packages/config" --json   # answer / follow-up
python3 $CDX send <task> --now "Stop, wrong approach. Refactor X instead" --json  # interrupt a running task first
```

`send` refuses while a task is running; that refusal is the guard against accidental interrupts. Reach for `--now` deliberately, when the thinking stream shows a wrong turn, not because you're impatient.

## When unsure what a task is doing

Escalate probes in cost order, and stop at the first one that settles wait-vs-steer-vs-kill:

```bash
python3 $CDX status <task> --json            # cheap: state, last-output age, last activity line
python3 $CDX peek <task> --json              # summarized recent events (commands, file changes, messages)
python3 $CDX peek <task> --thinking --json   # emergency only: ~1000 chars of live reasoning stream
```

`--thinking` is raw model stream and pays its tokens. Never poll it. One look, decide, act.

## Fan-out

Parallel tasks are the point: separate repos (or non-overlapping dirs), one spawn each, one `list` to watch them all. Two tasks writing the same checkout will trample each other, so cdx warns on spawn; take the warning seriously.

Keep the user oriented while tasks are in flight. With more than one task, show a compact plan table after spawning and at each check-in: task + one-line goal, backend/model/effort, status (running / waiting on X / done). A single task needs a sentence, not a table.

## Backends, models, effort

`spawn --backend codex|claude|grok` (default codex): identical verbs, states, and roles across all three. `--effort medium|high|max` (default high) is the per-task power dial; cdx resolves it to each backend's current model and reasoning settings. Never pick provider model names or raw reasoning levels for codex yourself — that translation is cdx's job. If the user asks what actually ran, the JSON output of every verb reports the resolved `model` and `provider_effort`.

**Fable is user-directed only.** Never select `fable` or `claude-fable-5` from task shape, cost, taste, or review heuristics; as a subagent it is normally too expensive. Spawn it only when the user explicitly asks for Fable (`--backend claude --model fable`); effort translation is handled by cdx as usual.

Pick by task shape along cost / taste / intelligence. These are defaults with reasons; deviate when the task tells you to:

| Task shape | Default | Why |
|---|---|---|
| taste-heavy: prose, frontend/UI, API design, anything that must *feel* right | claude / opus / high | taste: Anthropic models have the strongest judgment for language and aesthetics |
| general coding: features, refactors, bug fixes, tests | codex / high, with grok as the equal-footing budget alternative | intelligence per cost for the workhorse |
| explore + mechanical work: codebase questions, migrations, format churn | codex / medium, or claude / sonnet / medium | cost and speed; the intelligence bar is lower |
| review | high effort with a **different provider than the one that built**: claude / opus when codex built; codex / high when claude or grok built | cross-review catches what self-image misses; Sonnet is for exploration, not the default reviewer |
| computer-use / E2E verification | codex / high, **always codex** | the codex harness is by far the strongest at driving UIs; this pin is part of the role, not a preference |

`--model` overrides a single task; machine-level defaults live in `cdx config` (self-describing via `--help`); touch those only when the user asks. Note: the grok stream does not surface tool calls, so `peek` and `last_activity` are sparser for grok tasks than for codex/claude (state and results are unaffected).

## Housekeeping

`python3 $CDX doctor` before first use of a session if anything smells off (binary, state dir, orphans). `python3 $CDX clean --terminal` once results are harvested: a lean task list keeps `list` readable.

When the user asks to update this skill, read references/update.md and follow it: it fetches the latest published version, shows the user what changed, and applies it safely.
