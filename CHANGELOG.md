# Changelog

## 0.6.0 (2026-07-14)

- Make `clean` owner-scoped so parallel sessions stop clobbering each other. The task registry (`~/.codex-agents/tasks`) is shared by every session on the machine, and `clean --terminal`/`--all` used to sweep it globally, deleting a sibling session's finished-but-uncollected results (and, with `--all`, its still-running tasks). This is backend-agnostic: codex, claude, and grok tasks all shared the one registry and the one sweep.
- Each task now records an `owner` at spawn (`CDX_OWNER` if set, else the resolved cwd, so separate worktrees isolate with zero config). `clean --terminal`/`--all` only remove tasks owned by the current session; `owner` is surfaced in `status`/`list`, and the JSON reports a `skipped_foreign` count.
- Add `clean --any-owner` to restore the global sweep as a deliberate opt-in (also the only way to reap pre-owner legacy tasks). `kill`/`clean --task` were already correctly single-target and are unchanged.
- `clean` now refuses to remove a still-running (or still-starting) task: `clean --task` on one errors with "kill it first", and `--terminal`/`--all` skip it (reported as `skipped_running`). Interrupting-then-deleting a live task raced the detached supervisor, which could resurrect the directory as a partial `failed` entry or orphan the backend, so clean only ever reaps terminal tasks now, the same rule `send` follows. Kill a task first (`cdx kill`) if you want it gone.
- Harden the detached supervisor against resurrecting a cleaned task: it finalizes state through a new non-creating write, so if `clean` removed the directory in the meantime the write fails cleanly instead of re-creating a stale task. Read paths (`status`/`list`/`result`) that opportunistically persist derived state no longer resurrect a cleaned task either. `clean` re-checks each task is still terminal immediately before removing it, and reports a genuine `rmtree` failure instead of miscounting it as a skipped running task.
- SKILL.md: replace the blanket "`clean --terminal` once results are harvested" housekeeping advice with the owner-scoping contract and the `CDX_OWNER` override.

## 0.5.0 (2026-07-11)

- Rework the two-axis review: the "Standards" axis becomes **Correctness & Quality** (`roles/review-correctness.md`), which hunts behavioral bugs first, then repo conventions and the Fowler smell baseline. Both axes adopt P0–P3 severity (with anti-inflation calibration keyed to "would this block the merge?"), a mandatory residual-risk close, and judgment-first framing so the smell baseline stays a floor, not a nitpick checklist.
- Simplify the `FINDINGS:` line on both review axes to a plain completion sentinel (`FINDINGS: <n>` / `none`): it proves a detached run finished, it is not a count to parse — the orchestrator's adjudication reads the findings themselves.
- Lower the review effort default from `max` to `high`.
- SKILL.md accuracy pass, found by the new reviewer and verified against `cdx.py`: document the `kill <task>` verb; note that exit `12` (stalled) comes only from `status`/`peek` while `result` collapses stalled into `13`; scope model-alias pinning to codex (claude/grok forward the alias to the provider CLI); flag that a grok task stalled or interrupted on its first turn has no resumable session; correct "reaps true hangs" to the inactivity timeout it actually is.

## 0.4.0 (2026-07-09)

- Replace effort-based Codex auto-routing with symmetric model tiers: the agent picks `sol|terra` on codex exactly like `opus|sonnet` on claude; cdx pins the aliases to concrete model IDs (`gpt-5.6-sol`/`gpt-5.6-terra`, default sol).
- Make `--effort` a uniform reasoning dial across backends: medium→medium, high→high, max→xhigh (grok caps at high); each provider's very top tier (Claude max, Codex ultra) stays deliberately unreachable. Default effort drops from high to medium.
- Task-shape defaults follow suit: general coding codex/sol/high (sol/medium for genuinely simple tasks), explore codex/terra/medium (or claude/sonnet/medium), taste-heavy claude/opus/max, review at max effort with the opposite provider of the builder, computer-use codex/terra/high.

## 0.3.0 (2026-07-09)

- Keep the agent-facing effort vocabulary at `medium | high | max`, but route automatic Codex work deterministically: medium to GPT-5.6 Terra/high, high to GPT-5.6 Sol/high, and max to GPT-5.6 Sol/xhigh.
- Add model-aware Fable 5 effort translation (medium to low, high to medium, max to xhigh) and make Fable explicitly user-directed only in the skill policy.
- Report `model`, cdx `effort`, and resolved `provider_effort` in spawn, list, status, result, and send JSON so model routing is auditable.
- Keep SKILL.md agent-first: the orchestrator sees only backend/effort per task shape; the concrete Terra/Sol and Fable routing tables live in the README and the `--effort` CLI help, not in agent context.
- Make cross-provider review explicit: claude/opus reviews codex-built changes, codex/high reviews claude- or grok-built changes; Sonnet remains an exploration option.

## 0.2.1 (2026-07-09)

- SKILL.md: while multiple tasks are in flight, the agent shows a compact plan table (task + goal, backend/model/effort, status) after spawning and at each check-in; a single task gets a sentence instead.

## 0.2.0 (2026-07-09)

First public release.

- Add `roles/computer-use.md`: an E2E verification role that drives the running product (browser flows, screenshots, runtime state) and reports VERDICT/STEPS/OBSERVED-vs-EXPECTED per flow; pinned to the codex backend (gpt-5.5), whose harness is strongest at computer use.
- Add `roles/frontend.md`: a frontend role with a brownfield/greenfield gate. Brownfield treats the surrounding design as the spec (no foreign bodies), greenfield distills Anthropic's frontend-design guidance (deliberate choices, one signature element, no AI-default looks); both enforce "everything earns its place".
- Add a model-selection table to SKILL.md: per-task-shape defaults with reasons along cost / taste / intelligence (taste-heavy → claude/opus, general coding → gpt-5.5 or grok, explore/mechanical → gpt-5.5 medium, review → cross-model at high effort).
- Make `cxcc-subagent` path examples install-location agnostic by anchoring them to the skill's announced base directory.
- Add Grok Build CLI (`grok`) as a third `cdx` backend alongside codex and claude: `spawn --backend grok`, resume, and config key `model.grok`. Backend selection is now a per-backend adapter registry.
- Add `roles/explore.md`: a read-only exploration role with a fixed ANSWER/EVIDENCE/GAPS report contract, modeled on the explore-agent designs of Claude Code and Codex.
- Add references/update.md: self-update procedure. On request, the agent fetches the latest published skill version, reports what changed, and applies it.
- Remove the frozen build spec from `references/`: it was a construction artifact, not runtime knowledge. The exit-code table moves inline into SKILL.md; `scripts/cdx.py` is the source of truth for JSON schemas.
- Fix resuming dead turns: `send` on a stalled task no longer requires `--now` (the watchdog already killed the process), and a resume after a stalled/failed/killed turn replaces that turn's accounting slot. Previously such tasks could never reach `done` again.

## 0.1.0

- Initial private release with the `cxcc-subagent` skill.
