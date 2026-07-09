# Changelog

## Unreleased

- Make `cxcc-subagent` path examples install-location agnostic by anchoring them to the skill's announced base directory.
- Add Grok Build CLI (`grok`) as a third `cdx` backend alongside codex and claude: `spawn --backend grok`, resume, and config key `model.grok`. Backend selection is now a per-backend adapter registry.
- Add `roles/explore.md`: a read-only exploration role with a fixed ANSWER/EVIDENCE/GAPS report contract, modeled on the explore-agent designs of Claude Code and Codex.
- Add references/update.md: self-update procedure — on request, the agent fetches the latest published skill version, reports what changed, and applies it.
- Fix resuming dead turns: `send` on a stalled task no longer requires `--now` (the watchdog already killed the process), and a resume after a stalled/failed/killed turn replaces that turn's accounting slot — previously such tasks could never reach `done` again.

## 0.1.0

- Initial private release with the `cxcc-subagent` skill.
