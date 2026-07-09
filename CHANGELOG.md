# Changelog

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
