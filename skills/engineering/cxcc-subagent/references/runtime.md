# Runtime and recovery

## Monitoring

Use `wait` for harnesses that cannot turn background output into a new agent turn. It returns on a change, timeout, or when the fleet is idle. A tool call yielding a live process handle is still running: poll the same handle; start the next `wait` only after it returns.

Use `watch --json` when the harness provides a completion-aware or line-driven notification facility (for example Claude Code Monitor). Arm one watcher for the session. Confirm its `armed` event includes the expected owner and tasks. Keep it attached to that notification facility; a detached process alone cannot notify the agent.

Events disclose changes and escalated questions. Heartbeats confirm liveness while work continues. A timeout leaves workers untouched. `result --wait` is the single-task alternative.

## Ownership and cleanup

The registry is shared across sessions. Fleet commands default to the current owner. `-C <repo>` additionally includes that repository's tasks across owners; `--any-owner` explicitly expands to the whole machine. Use these when recovering work from another session. Inspect scope before cleaning foreign results.

`clean` removes terminal tasks and preserves live workers. Collect results before cleanup. A killed task remains resumable until cleaned.

## Backend limits

Grok exposes less activity than Codex or Claude, so a long tool call more readily raises `stall_suspect` and gives less evidence to judge. Inspect `peek` once. Grok publishes its resumable session ID only after a completed first turn: if that first turn stalls or is interrupted, spawn a fresh worker. Codex and Claude can resume an interrupted first turn once a session ID exists.

Successful CLI reads exit 0 regardless of task state. Shell failure means the call failed; inspect the structured error. TOON is the default; use JSON for programmatic parsing and streaming monitors. `status --full` provides process and usage diagnostics; `list --full` provides execution and ownership details.
