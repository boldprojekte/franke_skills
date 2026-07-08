# Franke Skills

Agent skills for practical engineering workflows.

This repo starts with `cxcc-subagent`: a cross-tool subagent supervisor for running Codex CLI and Claude Code as focused worker agents while a stronger model keeps orchestration, steering, and review in the main session.

The bet is simple: newer orchestration-grade models, including Fable-class models, are very good at planning, decomposing, and judging work. Coding work still benefits from isolated executors with their own context, logs, tests, and failure states. Cross-agent workflows let the orchestrator stay in control while delegating token-heavy implementation, review, and investigation to cheaper or more specialized workers.

## Quickstart

Install from this repository with the skills installer:

```bash
npx skills@latest add <github-user>/franke_skills
```

While the repository is private, install from a checked-out copy or from the private GitHub URL supported by your local installer.

## Skills

### `cxcc-subagent`

Spawn, monitor, steer, and collect detached coding subagents through `scripts/cdx.py`.

Use it when you want an orchestrating agent to fan out implementation, refactor, test-writing, investigation, or review work while the main session keeps moving. It stores task state under `~/.codex-agents` by default and requires Python 3.10+ plus the target backend CLI on `PATH`.

Supported backends:

- Codex CLI via `codex exec --json`
- Claude Code via `claude -p --output-format stream-json`

Platform support:

- macOS: tested locally
- Linux: expected to work; should be CI-tested before public release
- Windows: experimental until the process supervisor passes on `windows-latest`

## Repository Layout

```text
.claude-plugin/plugin.json
skills/engineering/cxcc-subagent/SKILL.md
skills/engineering/cxcc-subagent/references/
skills/engineering/cxcc-subagent/scripts/cdx.py
skills/engineering/cxcc-subagent/scripts/tests/test_cdx.py
```

## Development

Run the fast local test suite:

```bash
python3 -m unittest -v \
  test_cdx.StateDerivationTests \
  test_cdx.TurnAccountingRaceTests \
  test_cdx.WatchdogTests \
  test_cdx.CliSubprocessTests
```

from:

```bash
skills/engineering/cxcc-subagent/scripts/tests
```

The real backend smoke tests are intentionally separate because they require installed and authenticated agent CLIs.

## Credits

Created and maintained by Jan Franke.

Small references: Matt Pocock's public skills repo helped shape the lightweight skill-catalog structure and code-review subagent framing; Peter Steinberger's `codex-first` skill inspired treating Codex as a focused coding workhorse.

## License

MIT
