# Franke Skills

Keep the smart model in the chair. Send the coding to detached workers.

Newer orchestration-grade models (Fable-class and up) are excellent at planning, decomposing, and judging work. Actual coding still runs best in isolated executors with their own context, logs, tests, and failure states. This repo gives the orchestrator a way to stay in control while it fans out the token-heavy implementation, review, and investigation to cheaper or more specialized workers.

The first skill, `cxcc-subagent`, wraps Codex CLI, Claude Code, and Grok Build CLI as detached coding subagents behind one small, agent-first CLI.

## Why this over `codex exec` directly

Three things you don't get from spawning a raw coding CLI in a loop:

1. **A stuck run comes back as state, not silence.** A watchdog reaps true hangs, and when a worker needs input it surfaces as an explicit `awaiting_reply` instead of dying quietly in a log. The orchestrator polls attention-first: `awaiting_reply` / `failed` / `stalled` sort to the top.
2. **Three backends, one interface.** Codex CLI (`codex exec --json`), Claude Code (`claude -p --output-format stream-json`), and Grok Build CLI (`grok --prompt-file --output-format streaming-json`) run through the same verbs. Pick the workhorse per task; the orchestrator doesn't change.
3. **Review is built in, not improvised.** Composable role prompts include two-axis code review, against repo standards and against the spec the change was built from, run in parallel and adjudicated.

Division of labour stays honest: Codex types, you think. Spec before, review after.

## Quickstart

Install for Codex:

```bash
npx skills@latest add boldprojekte/franke_skills \
  --skill cxcc-subagent \
  --agent codex \
  --copy -y
```

Install for Codex and Claude Code:

```bash
npx skills@latest add boldprojekte/franke_skills \
  --skill cxcc-subagent \
  --agent codex claude-code \
  --copy -y
```

That writes the skill to `.agents/skills/cxcc-subagent` for Codex and `.claude/skills/cxcc-subagent` for Claude Code.

Needs Python 3.10+ and at least one backend CLI (`codex`, `claude`, or `grok`) on your `PATH`. Task state lives under `~/.codex-agents` by default.

## The loop

```bash
CDX=".agents/skills/cxcc-subagent/scripts/cdx.py"
ROLES=".agents/skills/cxcc-subagent/references/roles"

# 1. Spawn a worker from a role + a work order - returns instantly, runs detached
python3 $CDX spawn -f $ROLES/general.md -f task.md -C /path/to/repo --json

# 2. Go do other things. The run stays alive across everything short of a machine restart.

# 3. Check in at natural pauses - attention-first, not on a poll loop
python3 $CDX list --json

# 4. Collect, then verify it yourself: read the diff, run the proof command
python3 $CDX result <task> --json
```

Every verb takes `--json`: stdout is pure JSON, diagnostics go to stderr. Answering a worker's question and steering a wrong turn share one verb (`send`), same thread, full context retained.

When using the source checkout directly, replace `.agents/skills/cxcc-subagent` with `skills/engineering/cxcc-subagent`.

## Skills

### `cxcc-subagent`

Spawn, monitor, steer, and collect detached coding subagents via `scripts/cdx.py`.

Use it when an orchestrating agent should fan out implementation, refactors, test-writing, investigation, or review while the main session keeps moving.

Supported backends:

- Codex CLI via `codex exec --json`
- Claude Code via `claude -p --output-format stream-json`
- Grok Build CLI via `grok --prompt-file --output-format streaming-json`

Platform support:

- macOS: tested locally, CI-covered
- Linux: CI-covered
- Windows: experimental; not CI-gated yet

## Repository layout

```text
.claude-plugin/plugin.json
skills/engineering/cxcc-subagent/
  SKILL.md
  references/            # review contract, role prompts
  scripts/cdx.py         # the supervisor CLI
  scripts/tests/         # fast unit suite
```

## Development

Run the fast local test suite from `skills/engineering/cxcc-subagent/scripts/tests`:

```bash
python3 -m unittest -v \
  test_cdx.StateDerivationTests \
  test_cdx.TurnAccountingRaceTests \
  test_cdx.WatchdogTests \
  test_cdx.CliSubprocessTests
```

Real-backend smoke tests are intentionally separate; they need installed and authenticated agent CLIs.

## Credits

Created and maintained by Jan Franke.

Peter Steinberger's [codex-first](https://github.com/steipete/agent-scripts) skill inspired treating Codex as a focused coding workhorse. Matt Pocock's [skills](https://github.com/mattpocock/skills) repo helped shape the lightweight skill-catalog structure and the code-review subagent framing.

## License

MIT

## Usage: `list_skills.py`

`scripts/list_skills.py` scans `skills/` recursively for `SKILL.md` files and reads the `name` and `description` from each skill's YAML frontmatter. Stdlib-only; needs Python 3.10+ and runs from any cwd.

```bash
# JSON array of {category, name, description} to stdout
python3 scripts/list_skills.py

# the same data as a markdown table
python3 scripts/list_skills.py --markdown
```

`category` is the folder directly under `skills/` (e.g. `engineering`).
