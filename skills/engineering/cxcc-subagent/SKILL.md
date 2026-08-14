---
name: cxcc-subagent
description: "Spawns, monitors, steers, and collects detached Codex CLI, Claude Code, or Grok coding subagents via the cdx supervisor. Use whenever work is delegated to a subagent (implementation from a frozen spec, refactors, migrations, bug fixes, test writing, read-only codebase exploration, frontend/UI builds, E2E verification of a running app, or two-axis code review) and the session should keep working instead of blocking. Also triggers for: checking whether a delegated run is still alive, answering a question a subagent escalated, redirecting or killing a runaway run, fanning out parallel tasks, or updating this skill."
---

# CXCC Subagent

`cdx` supervises detached `codex exec` sessions. Spawn returns immediately, `watch`/`wait` bring state changes back to you as they happen, a watchdog reports a task that has gone quiet (no output for 5 min) so you can judge it, and escalated questions surface as an explicit state instead of dying in a log. Division of labour stays as in codex-first: Codex types, you think. Spec before, review after.

The CLI is `scripts/cdx.py` inside this skill folder; it runs from any cwd and needs only Python 3.10+.

```bash
SKILL_DIR=<this skill's base directory, announced when the skill loads>
CDX="$SKILL_DIR/scripts/cdx.py"
ROLES="$SKILL_DIR/references/roles"
```

Shell state does not persist between Bash calls, so re-declare these in every call that touches cdx.

The task registry is shared by every session on the machine, so every verb that sees or sweeps more than one task is scoped to an **owner**. cdx derives it from your harness's session id on its own (`CODEX_THREAD_ID`, `CLAUDE_CODE_SESSION_ID`). Check it once at setup with `python3 $CDX watch --once --json`, whose `armed` line reports the `owner`. If it reads as a filesystem path, your harness exposes no session id: then export `CDX_OWNER=<stable slug>` in every cdx call for the rest of the session, because the path fallback collides with any parallel chat in the same directory.

Every verb takes `--json`. Use it always; stdout is pure JSON, diagnostics go to stderr. Exit codes: 0 ok · 2 usage · 3 not found · 4 invalid state · 5 backend/internal · 7 binary missing · 10 working · 11 awaiting_reply · 12 stalled · 13 failed/killed. Caveat: `12` only comes from `status`/`peek`; `result` collapses a stalled task into `13`, so read the JSON `state` field, not the exit code, when you need to tell stalled from failed. If a JSON field is genuinely unclear, the source of truth is `scripts/cdx.py`.

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
| `roles/review-correctness.md` | reviewing a change for correctness (behavioral bugs) + repo conventions + smell baseline, severity-ranked | the review target file per references/review.md |
| `roles/review-spec.md` | reviewing a change against the plan/spec it was built from | the review target file per references/review.md |

**Explore tasks:** delegate only questions that would cost you more than a few directed searches; ask them specific and well-scoped, fan out parallel explorers for independent questions, and follow up on the same task via `send`. Trust the ANSWER/EVIDENCE/GAPS report. Don't re-run its searches.

**For any code review, read references/review.md first.** It defines the review contract (target, axes, and sources, all stated to the user before spawning), the target-file format, the parallel two-axis run, and the adjudication step. Don't improvise a review flow when that file exists.

## The loop

1. **Spawn.** Write the prompt as a work order. The worker has zero session context, so the work order carries exactly the **delta**: everything the worker needs that is NOT in the codebase. Decisions the user made in this session, constraints you learned, approaches already ruled out, verified facts the worker cannot rediscover. What IS in the codebase gets referenced by path, not repeated as text. And don't write the code in the prompt; you are delegating the typing, not dictating keystrokes. Structure: goal, repo + key paths, constraints ("don't touch X"), non-goals, proof expected (exact command, scoped to the worker's touched area — the one full-suite run is your own final gate after all tasks merge, never a per-worker proof), output shape. Then:
   ```bash
   python3 $CDX spawn -f prompt.md -C /path/to/repo --json    # returns {task, pid, state} instantly
   ```
   Done when: JSON came back with a task name. Do not wait here; move on.

2. **Set up how you learn about changes.** Which of the two you use is a property of your harness, not a preference: **can a line printed by a background process start a new turn while you sit idle?** In Claude Code it can (`Monitor`), so arm the stream once for the whole session and go; anywhere else it cannot, so you keep a turn open and block instead. Both emit the same owner-scoped events; `--help` on either has the details.
   ```bash
   python3 $CDX watch --json   # push: streams events, never exits. Arm once via Monitor(persistent: true), never re-arm, never run two
   python3 $CDX wait --json    # pull: blocks, returns once on the first event (or after 9 min, or at once when nothing runs), always exit 0. Call it again while work is in flight
   ```
   Events: `change` when a task leaves `working` (escalated `QUESTION:` inlined), `stall_suspect` / `recovered` when output dries up and returns, `heartbeat` every 10 minutes while anything works, so the session never goes quiet mid-flight. Neither ever touches a worker.
   On pull, your harness may hand the call back before it finished — Codex yields a live session handle after 30s, and a continuation poll caps out at 300s. **An empty return with the session still alive is not a result**: keep polling that same session until the JSON arrives. `wait` outlives every one of those yields; do not start a second one.
   Done when: the `armed` line came back — or the first `wait` returned — listing the tasks you expect.

3. **Work on something else** (push), or keep the wait loop running (pull). The task runs detached and survives anything short of a reboot.

4. **Act on each event.** A `change` is the trigger: read its `state` and do the matching move from the table below. A `heartbeat` or a `timeout` needs no action unless its `uncollected` list holds something. For an explicit look between events:
   ```bash
   python3 $CDX list --json    # attention-first: awaiting_reply / failed / stalled sort to the top
   ```
   `list` shows only this session's tasks; parallel chats' tasks appear solely as a `skipped_foreign` count, so they never leak into your check-ins (scoping details in Housekeeping).
   Done when: every task is accounted for: `working` tasks left alone, everything else acted on (below).

5. **Collect and verify.**
   ```bash
   python3 $CDX result <task> --json    # exit 0 done · 11 awaiting_reply · 13 failed · 10 still working
   ```
   Collect when `watch`/`wait` reports the task left `working`; there is nothing to gain from calling `result` before that. `result <task> --wait --json` blocks on one single task and is only for when that one task is all you are waiting on — for a fleet, `wait` is the right blocking call. Exit 10 from either form means "still running, untouched", never "died, resume it".
   A result is not an outcome: `git status -sb` + read the full diff in the repo, judge it like a contributor PR. Codex claims are advisory — but the answer to that is evidence, not repetition. The report carries the proof's real output: plausibilize it against the diff instead of re-running it, and re-run the targeted proof only on a suspicion trigger — output missing or vague, output inconsistent with the diff (tests named that don't exist, counts that don't add up), a failed spot-check. Note whether the credited proof is subsumed by your own final gate (step 6); non-subsumed proofs are re-run once there, never per collection. Done when: the diff is reviewed and the evidence is credited — or the targeted re-run passed.

6. **Close the gate — once, after the last task.** With all tasks merged, run the full-suite gate the per-worker proofs deliberately skipped, plus a one-time re-run of every credited proof noted as not subsumed by it (manual checks, benchmarks, external-integration tests). Done when: the gate ran green and no non-subsumed note is left open.

## Acting on states

| State | Meaning | Your move |
|---|---|---|
| `working` | bytes still flowing | leave it alone |
| `awaiting_reply` | Codex escalated a `QUESTION:` | read it in `status`/`result`, answer via `send` |
| `done` | turn finished cleanly | collect, review, verify |
| `failed` | turn errored or process died | `peek` for the tail, then `send` a fix or respawn |
| `working` + `stall_suspect` | no output for 5 min, **process still running and untouched** | judge it, don't reflex-kill: `peek` once. A long build or test run is silent and fine; a worker that is genuinely stuck gets `send --now` or `kill`. Ignoring it is a valid answer too |
| `stalled` | the hard limit finally killed a task that never spoke again (1 h) | `send "continue"` resumes exactly where it stopped |
| `killed` | you killed it | resumable via `send` |

Answering and steering share one verb (same thread, full context retained):

```bash
python3 $CDX send <task> "Use the existing Zod schema in packages/config" --json   # answer / follow-up
python3 $CDX send <task> --now "Stop, wrong approach. Refactor X instead" --json  # interrupt a running task first
```

`send` refuses while a task is running; that refusal is the guard against accidental interrupts. Reach for `--now` deliberately, when the thinking stream shows a wrong turn, not because you're impatient.

Kill a runaway outright with `python3 $CDX kill <task> --json`; the process stops, the task moves to `killed`, and it stays resumable via `send`.

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

`spawn --backend codex|claude|grok` (default codex): identical verbs, states, and roles across all three. Every task has two dials, with the same mental model on every backend:

- **Model tier** via `--model`: `opus|sonnet` on claude, `sol|terra` on codex (default `sol`). Always use these stable aliases, never raw provider model names. On codex, cdx pins the concrete provider model behind `sol`/`terra`; grok has a single pinned model and needs no `--model` at all; on claude the alias is forwarded to the provider CLI, which resolves it there.
- **Effort** via `--effort medium|high|max` (default `medium`): the reasoning dial, translated uniformly to `medium|high|xhigh` on every backend. The `max` tier that codex and claude offer above `xhigh` stays deliberately outside this surface: it is expensive and rarely pays off. `high` is the good default for real work.

If the user asks what actually ran, the JSON output of every verb reports `model` and `provider_effort`. On codex and grok `model` is the resolved concrete id; on claude it's the alias you passed (or `null` if you let the provider default it), since the provider CLI does the resolving.

**Fable is user-directed only.** Never select `fable` or `claude-fable-5` from task shape, cost, taste, or review heuristics; as a subagent it is normally too expensive. Spawn it only when the user explicitly asks for Fable (`--backend claude --model fable`); effort translation is handled by cdx as usual.

Pick by task shape along cost / taste / intelligence. These are defaults with reasons; deviate when the task tells you to:

| Task shape | Default | Why |
|---|---|---|
| taste-heavy: prose, frontend/UI, API design, anything that must *feel* right | claude / opus / max | taste: Anthropic models have the strongest judgment for language and aesthetics; this is where the top dial earns its cost |
| general coding: features, refactors, bug fixes, tests | codex / sol / high; drop to sol / medium when the task is genuinely simple; grok as the equal-footing budget alternative | intelligence per cost for the workhorse |
| explore + mechanical work: codebase questions, migrations, format churn | codex / terra / medium, or claude / sonnet / medium | cost and speed; the intelligence bar is lower |
| review | `high` effort with a **different provider than the one that built**: claude / opus / high when codex built; codex / sol / high when claude or grok built | cross-review catches what self-image misses; Sonnet is for exploration, not the default reviewer |
| computer-use / E2E verification | codex / terra / high, **always codex** | the codex harness is by far the strongest at driving UIs; this pin is part of the role, not a preference; driving flows needs stamina, not deep reasoning |

Machine-level defaults live in `cdx config` (self-describing via `--help`); touch those only when the user asks. Note: the grok stream does not surface tool calls, so `peek` and `last_activity` are sparser for grok tasks than for codex/claude (state and results are unaffected). That also thins out `stall_suspect` for grok: a grok worker inside a long tool call goes fully silent, so it raises the flag more readily *and* the event carries less to judge it by. There, `peek` once instead of reading the event. One grok-only edge: a grok task that stalls or is interrupted during its *first* turn has no resumable session yet (the session id only lands on a completed turn), so `send` can't resume it — respawn instead. Codex and claude resume cleanly from a first-turn stall.

## Housekeeping

`python3 $CDX doctor` before first use of a session if anything smells off (binary, state dir, orphans). `python3 $CDX clean --terminal` once results are harvested: a lean task list keeps `list` readable.

`list`, `watch`, `wait`, and `clean` are **owner-scoped**: by default they see and touch only tasks stamped with this session's owner (derived as described in the setup block). Foreign tasks surface only as a `skipped_foreign` count. Both verbs share two escape hatches: `--any-owner` is the deliberate machine-wide view/sweep — on `list` it answers "what else is running on this machine", on `clean` use it only when you know no sibling session has results in flight (it is also the only way to reap pre-owner legacy tasks). `-C <repo>` additionally covers that repo's tasks across owners — needed when tasks were spawned with `-C` from a different directory (the owner is the spawning cwd, not the `-C` target). `clean` only removes tasks that are already terminal. A running (or still-starting) task is never deleted (`clean --task` on one errors, `--all` reports it as skipped), so kill it first (`cdx kill <task>`) if you really want it gone, the same rule `send` follows.

When the user asks to update this skill, read references/update.md and follow it: it fetches the latest published version, shows the user what changed, and applies it safely.
