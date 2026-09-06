---
name: cxcc-subagent
description: "Delegate coding work to detached Codex, Claude Code or Grok workers with cdx. Use for starting delegated work, monitoring or steering existing workers, and collecting their results."
---

# CXCC Subagent

`cdx` supervises detached workers. You own the work order, decisions, review and final verification; workers execute bounded tasks with their own context.

## Start

The CLI is `scripts/cdx.py` inside this skill directory and needs Python 3.10+. Invoke it by its absolute path. Run it without arguments to see this session's tasks and discover commands. Command-specific `--help` owns flags, defaults, output formats and examples.

Owner is derived from the harness session ID. If the home view shows a filesystem path as `owner`, set `CDX_OWNER` to a stable, session-specific value on every call to isolate parallel chats in the same directory.

## Delegate and collect

1. **Bound the work.** Give each worker a goal, repository and relevant paths, constraints, expected proof and output shape. Include decisions from this conversation that the worker cannot find in the repo. Reference existing material by path. Concurrent writers need separate worktrees or disjoint file ownership.
2. **Spawn.** Pass a role file followed by the work order using repeatable `-f`. The returned task name identifies the worker; use it exactly. Continue independent work after dispatch.
3. **Observe.** Use `wait` while workers run. If the harness yields a live process handle, poll that same process until the response arrives. Use `watch --json` instead only when the harness can deliver background lines as agent notifications; arm one watcher for the session. For watcher setup, cross-session recovery or backend-specific resume limits, read [runtime.md](references/runtime.md).
4. **Act.** Read `state`: `working` needs time; `awaiting_reply` needs an answer through `send`; `done` needs collection through `result`. For `failed`, `stalled` or `killed`, inspect `peek` and choose a corrected follow-up or a fresh worker. A `stall_suspect` flag means the worker is still running: inspect once and judge whether to wait or redirect. Use `send --now` for deliberate interruption.
5. **Verify.** Read the result and actual repository diff. Credit concrete worker test evidence when it matches the change; re-run targeted checks for missing or contradictory evidence. After integration, run the full required gate once and any credited checks it does not cover. Finish when every task is accounted for and the integrated outcome is proven. Clean terminal tasks after collecting their results.

While workers run, keep the user informed of material progress and decisions. With multiple workers, summarize task, purpose, model/effort and status together.

## Roles

A role is a prompt block that frames what kind of agent the task is; `-f` is repeatable and concatenates in order, so compose role first, task second:

```bash
python3 <skill-dir>/scripts/cdx.py spawn -f <skill-dir>/references/roles/general.md -f task.md -C <repo>
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

## Backends, models, effort

`spawn --backend codex|claude|grok` (default codex): identical verbs, states, and roles across all three. Every task has two dials, with the same mental model on every backend:

- **Model tier** via `--model`: codex `sol|terra` (default `sol`), claude `opus|sonnet`; grok uses its pinned default. Use stable aliases; cdx owns model resolution.
- **Effort** via `--effort medium|high|max` (default `medium`); cdx translates it per model. Use `high` for real work.
- **Astra and Fable are user-directed only.** Select codex `astra` or claude `fable` when the user explicitly requests that model.

Execution details are available as `model` and `provider_effort`. Claude aliases other than `fable` are resolved by the provider CLI.

Pick by task shape along cost / taste / intelligence. These are defaults with reasons; deviate when the task tells you to:

| Task shape | Default | Why |
|---|---|---|
| taste-heavy: prose, frontend/UI, API design, anything that must *feel* right | claude / opus / max | taste: Anthropic models have the strongest judgment for language and aesthetics; this is where the top dial earns its cost |
| general coding: features, refactors, bug fixes, tests | codex / sol / high; drop to sol / medium when the task is genuinely simple; grok as the equal-footing budget alternative | intelligence per cost for the workhorse |
| explore + mechanical work: codebase questions, migrations, format churn | codex / terra / medium, or claude / sonnet / medium | cost and speed; the intelligence bar is lower |
| review | `high` effort with a **different provider than the one that built**: claude / opus / high when codex built; codex / sol / high when claude or grok built | cross-review catches what self-image misses; Sonnet is for exploration, not the default reviewer |
| computer-use / E2E verification | codex / terra / high, **always codex** | the codex harness is by far the strongest at driving UIs; this pin is part of the role, not a preference; driving flows needs stamina, not deep reasoning |

Machine-level defaults belong to `config`; change them only on user request. For installation or backend trouble, use `doctor`.

When the user requests a skill update, read [update.md](references/update.md).
