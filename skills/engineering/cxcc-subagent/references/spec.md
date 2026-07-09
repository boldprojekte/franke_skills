# Spec: `cdx`, the Codex task supervisor CLI

Status: FROZEN v2.1 (approved by Jan, 2026-07-08). Target: built by Codex from this frozen spec; SKILL.md written separately by Claude.

## v2.1 amendment: unified effort vocabulary + model config

**Schemas.** Since v2.0 every task payload (`spawn`, `send`, `list` rows, `status`, `result`, `peek`) additionally carries `"backend": "codex"|"claude"`; this supersedes the JSON shapes listed in the original command sections below. `kill` on an already-terminal task is a strict no-op: it reports the existing state and does not rewrite it to `killed`.

**Effort.** `--effort` on `spawn` accepts exactly `medium|high|max` (default `high`). It is cdx-level vocabulary, translated per backend at command build time:

| cdx | codex (`model_reasoning_effort`) | claude (`--effort`) |
|---|---|---|
| `medium` | medium | high |
| `high` | high | xhigh |
| `max` | xhigh | max |

Claude's own `medium` is deliberately unreachable. Any other value → exit 2 naming the three valid choices. `meta.json` stores the cdx-level value; translation happens where the backend command is assembled (spawn AND resume).

**Model config.** New verb `cdx config`:

- `cdx config get [--json]`: print current config (empty object if none).
- `cdx config set <key> <value>` / `cdx config unset <key>`: allowed keys are `model.codex` and `model.claude`.
- Storage: `<state-dir>/config.json` (so `~/.codex-agents/config.json` by default). Machine-level, applies to all cdx spawns on this machine and nothing else.

Model resolution at spawn: `--model` flag > `config.json` `model.<backend>` > backend's own built-in default (config ships empty; an unset key means "let the backend decide"). The resolved model is recorded in `meta.json` as today; resume reuses the model recorded at spawn, not the current config (a task keeps its model across turns).

## v2.0 amendment: Claude Code as second backend

`spawn` gains `--backend codex|claude` (default `codex`); the choice is stored in `meta.json` (`backend`) and every later verb (`send`, `status`, `result`, `peek`) branches on it. All verbs, states, exit codes, the state dir layout, the watchdog, and the escalation preamble stay identical.

**Claude adapter:**

- Binary: `claude` via PATH, env override `CDX_CLAUDE_BIN`; missing → exit 7 with install hint. `doctor` checks both backends; a missing `claude` is reported as a warning, not a failure (codex-only setups stay green).
- Spawn command (cwd = repo, Claude has no `-C` flag):
  `claude -p --output-format stream-json --verbose --include-partial-messages --dangerously-skip-permissions [--model M] [--effort E] < prompt-file`
  Prompt goes via stdin (text-mode stdin merge); never via argv (quoting/size). `--include-partial-messages` is mandatory: without it a long thinking phase emits zero bytes and the byte-growth watchdog would false-kill.
- Resume: `claude -p --resume <session_id> --output-format stream-json --verbose --include-partial-messages --dangerously-skip-permissions < prompt-file`, cwd = repo. Never call `--resume` without an explicit id.
- Session id: every NDJSON line carries `session_id`; capture from the first parsed line into meta `thread_id` (same field as codex's thread id).
- Event mapping for state derivation: a line with `type: "result"` is the turn-terminal marker (counts as `turn.completed`); `result.is_error == true` or an error `subtype` maps to `failed`. The final agent message is `result.result` (string). Fall back to the last `assistant` message text. `stream_event` lines count only as liveness bytes, not as items.
- `peek`: summarize `assistant`/`user`/`system` lines analogous to codex items (tool uses condensed, texts truncated). `peek --thinking`: Claude writes no stderr thinking stream. Reconstruct the tail by concatenating text deltas from the most recent `stream_event` lines in events.jsonl, same char cap.
- `--effort` passes through per backend (codex: low|medium|high|xhigh; claude: low|medium|high|max). Validate against the chosen backend's set and reject the other's odd value with exit 2 naming the valid choices.
- stderr.log for claude contains only real warnings/errors; liveness remains combined byte growth over events.jsonl + stderr.log (stream_event deltas keep events.jsonl growing during thinking).

## v1.2 amendment: composable prompts

`-f/--file` on `spawn` and `send` is repeatable; file contents are concatenated in argument order, separated by a blank line. `-` (stdin) may appear at most once among them. Positional PROMPT and `-f` remain mutually exclusive. This enables role blocks (`references/roles/*.md` in the skill) composed ahead of the task-specific prompt.

## v1.1 amendment: turn accounting (fixes the send/result race)

`meta.json` gains `turns_launched` (set to 1 on spawn, incremented by every `send`) and `turn_launched_at` (timestamp of the latest spawn/send). State derivation MUST gate turn-terminal states on turn count: the task is `done`/`awaiting_reply` only when the number of `turn.completed` events in events.jsonl is >= `turns_launched`. While the count is < `turns_launched`: state is `working` if the pid is alive OR `turn_launched_at` is less than 15s ago (helper startup grace); otherwise `failed`. This guarantees that `result --wait` issued immediately after `send` blocks until the NEW turn finishes instead of returning the previous turn's message.

Also in v1.1: the watchdog helper must not re-parse events.jsonl on every 0.2s poll tick; read events only until thread_id is captured, then rely on combined byte size for liveness (existing 30s cadence). `events_last_60s` is replaced by `output_bytes` (combined size of events.jsonl + stderr.log) in status output.

## Purpose

Agent-first CLI that lets an orchestrating agent (Claude Code) spawn, monitor, steer, and collect multiple parallel Codex sessions. Replaces the blocking `codex exec` + temp-file recipes in the `codex-first` skill.

Backend: `codex exec --json` subprocesses (stable surface). No app-server, no daemon. The verb layer is backend-agnostic so the backend could be swapped later without changing the surface.

## Packaging

- Single Python 3.10+ script, **stdlib only**: `scripts/cdx.py` inside the skill folder.
- Runs from any directory; no cwd assumptions; all paths resolved absolute.
- Locates the codex binary via PATH, honoring an optional `CDX_CODEX_BIN` env override. Missing binary → exit 7 with install hint.

## State

```
~/.codex-agents/
└── tasks/<task-name>/
    ├── meta.json        # registry entry (locked writes)
    ├── prompt.md        # the exact prompt sent (incl. injected preamble)
    ├── events.jsonl     # captured stdout JSONL of codex exec (the liveness source)
    ├── stderr.log       # codex stderr (never parsed, kept for debugging)
    └── turns/           # one prompt file per follow-up send
```

`meta.json` fields: `task`, `repo` (abs path), `thread_id` (from `thread.started` event, null until seen), `pid`, `spawned_at`, `model`, `effort`, `state`, `turns` (int), `last_exit_code`.

- Task names: user-supplied via `--name` or auto-generated `<adjective>-<noun>` (e.g. `brisk-otter`). Lowercase, digits, hyphens. Name collision on spawn → exit 4, error suggests `--name` or `cdx send`.
- Concurrent-safe: per-task `meta.json` written atomically (write temp + rename). No global lock needed; the registry is the directory listing.

## Task state machine

Derived live (never trusted stale from meta.json alone):

| State | Meaning | Derivation |
|---|---|---|
| `working` | Codex is actively producing output | pid alive AND bytes still growing (see liveness) |
| `stalled` | Watchdog killed a hung task | no byte growth for `--stall-after` → watchdog SIGINT, reason recorded in meta |
| `awaiting_reply` | Codex escalated a question | turn completed AND final agent message contains a line starting `QUESTION:` |
| `done` | Turn completed normally | `turn.completed` event, no QUESTION line |
| `failed` | Turn failed or process died | `turn.failed` / `thread.error` event, or pid dead without turn completion |
| `killed` | Explicitly killed via `cdx kill` | meta flag |

`awaiting_reply`, `done`, `failed`, `killed`, `stalled` are turn-terminal; all remain resumable via `send` (session persists in `~/.codex/sessions`).

### Liveness & watchdog

Liveness signal is **byte growth across `events.jsonl` + `stderr.log`** (codex streams its thinking to stderr continuously, so an honest long turn always grows; a hung process grows nothing). Event-boundary age alone is NOT used. Long reasoning is event-silent.

The detached process `spawn` creates stays alive as a tiny watchdog: every 30s it compares combined byte size; no growth for `--stall-after` seconds (default 300) → SIGINT the codex process (grace 10s, then SIGKILL), set state `stalled` with `stall_reason` and byte counters in meta, then exit. `--stall-after 0` disables the watchdog. An hour-long working task is never falsely killed (stderr keeps growing); a true hang is reaped after 5 minutes instead of blocking silently.

## Escalation protocol

`spawn` and `send` prepend this preamble to the prompt (suppress with `--no-preamble`):

```
[orchestration protocol] You are run non-interactively by an orchestrating
agent. If you hit a decision you cannot make yourself (missing access,
ambiguous requirement, a destructive or irreversible choice), do not guess:
end your turn with a line starting exactly `QUESTION: ` followed by what you
need to know. You will receive the answer as a follow-up message in this
same session. Otherwise end with a normal final summary of what you did and
how you verified it.
```

The supervisor classifies `awaiting_reply` by scanning the final agent message for a `QUESTION:` line and surfaces the question text verbatim in `status`/`list`/`result`.

## Commands

Global flags on every command: `--json` (machine output; stable schema, stdout=data stderr=diagnostics), `--state-dir <dir>` (default `~/.codex-agents`, or env `CDX_STATE_DIR`).

Human output (default) is compact single-line-per-item; SKILL.md will always use `--json`.

### `cdx spawn [PROMPT] [-f FILE | -] -C REPO [options]`

Start a new Codex task; return immediately.

- Prompt: positional string, `-f file`, or `-` for stdin. Exactly one source; none → exit 2.
- `-C/--repo DIR` required (no silent cwd default: agents must be explicit).
- `--name NAME` optional; `--model M`, `--effort low|medium|high|xhigh` (default high), `--no-preamble`, `--stall-after S` (default 300, 0 = no watchdog).
- Runs detached: `codex exec --json --dangerously-bypass-approvals-and-sandbox -C REPO -` with the (preamble+prompt) on stdin, stdout→`events.jsonl`, stderr→`stderr.log`. Double-fork so cdx exits instantly and the task survives the caller.
- Outside a git repo: pass `--skip-git-repo-check` through automatically after detecting non-repo (don't fail).
- Warning (stderr, non-fatal) if another non-terminal task already targets the same repo.

JSON out: `{"task","repo","pid","state":"working","model","effort"}`; exit 0.

### `cdx list [--all]`

All tasks in non-terminal states, plus terminal ones from the last 24h. `--all` = everything.

JSON out: array of `{"task","state","repo","age_s","last_output_age_s","last_activity","question"}` (`last_output_age_s` = seconds since last byte growth on events.jsonl/stderr.log) where `last_activity` is a one-line summary of the newest meaningful event (e.g. `command: pnpm test`, `msg: Implemented the parser…` first 80 chars) and `question` is the QUESTION text or null. Sorted: attention-needing states first (`awaiting_reply`, `failed`, `stalled`), then `working`, then rest.

### `cdx status TASK`

One task, detailed: everything from `list` plus `thread_id`, `pid_alive`, `turns`, `spawned_at`, event counts (`events_total`, `events_last_60s`), token usage if present in events.

Exit codes double as a cheap probe: 0 = `done`, 10 = `working`, 11 = `awaiting_reply`, 12 = `stalled`, 13 = `failed`/`killed`. (Distinct codes so a script can branch without parsing.)

### `cdx peek TASK [--tail N] [--thinking [CHARS]]`

Summarized recent activity, newest last, default N=15 items. One line per item: timestamp-age, item type, condensed content (commands with exit codes, file changes as paths, agent/reasoning messages truncated to 120 chars). Never dumps raw JSONL. `--full` prints the untruncated final agent message only.

`--thinking [CHARS]` (default 1000, max 1500): additionally return the last CHARS characters of the live thinking stream (`stderr.log` tail), ANSI-stripped. This is the **emergency probe** for "what is it doing *right now*" when a task is event-silent mid-turn. Deliberately capped and off by default: it's raw model stream, token-expensive, and not meant for routine polling (SKILL.md will say: use only when `status`/`peek` leave you genuinely unsure whether to wait, steer, or kill).

### `cdx result TASK [--wait] [--timeout S]`

The final agent message of the latest turn.

- Without `--wait`: if turn still running → exit 10 with message "still working; use --wait or peek".
- `--wait`: block until turn-terminal, poll events.jsonl (1s), `--timeout` default 3600 → exit 6 on timeout (task keeps running).
- JSON out: `{"task","state","message","question","turns","duration_s"}`. Exit: 0 done, 11 awaiting_reply, 13 failed.
- `result` is idempotent and read-only.

### `cdx send TASK [PROMPT] [-f FILE | -] [--now]`

Follow-up / answer / steering on the same thread.

- Refuses while task is `working`/`stalled` → exit 4, error says: "task is running; use --now to interrupt-and-redirect, or wait for result". With `--now`: SIGINT the process, wait ≤10s for exit (then SIGKILL), then resume.
- Resume mechanics: `codex exec resume <thread_id> --dangerously-bypass-approvals-and-sandbox --json -` run with cwd=repo (hides today's resume flag traps). New events append to `events.jsonl`; `turns` increments; state returns to `working`.
- Preamble: a one-line reminder (`[orchestration protocol] Same rules as before: escalate with QUESTION: if blocked.`) unless `--no-preamble`.

JSON out: same shape as spawn.

### `cdx kill TASK`

SIGINT → grace 10s → SIGKILL. State → `killed`. Idempotent: killing a terminal task is a no-op (exit 0, note on stderr). Task stays resumable via `send`.

### `cdx clean [--task NAME | --terminal | --all]`

Remove task dirs from the state dir (never touches `~/.codex/sessions`). `--terminal` = all turn-terminal tasks. No flags → exit 2 (must be explicit). `--dry-run` supported.

### `cdx doctor`

Checks, each with pass/fail + fix hint: codex on PATH + version; `codex exec --json` supported (probe `--help`); state dir writable; sessions dir exists; count of orphaned tasks (meta says working, pid dead); it offers that `status` auto-heals these to `failed`.

## Error contract

- Unknown task name: exit 3, `did you mean <closest>?` via edit distance; near-miss (distance ≤2) auto-corrects with a note on stderr.
- Every error message names cause AND next action. No tracebacks (top-level catch → exit 5 with one-line cause + path to stderr.log).
- Exit codes: 0 ok · 2 usage · 3 not found · 4 invalid state · 5 backend/internal · 6 timeout · 7 codex missing · 10-13 state probes (status/result as above).

## Non-goals (v1)

- No fork, no mid-turn steer (approximated by `send --now`), no app-server/daemon, no approval relaying (always full-bypass sandbox mode, matching today's `--yolo` house default), no TUI, no config file.

## Testing (Codex, before handoff)

1. Unit: state derivation from synthetic events.jsonl fixtures (all six states, QUESTION parsing incl. multiline); watchdog stall detection against a fake process that stops writing.
2. Real backend: spawn a trivial task in a temp repo ("create FILE.txt with content X, verify, summarize"), poll status → done, result contains message, file exists. A QUESTION round-trip: prompt that forces escalation → awaiting_reply → send answer → done. **Never skip when codex is missing: fail.**
3. CLI subprocess: run every verb from a different cwd, `--json` parses, exit codes match spec, stdout is pure JSON (no stray output).
