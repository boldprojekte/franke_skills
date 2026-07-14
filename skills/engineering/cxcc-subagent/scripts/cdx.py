#!/usr/bin/env python3
"""cdx: a tiny supervisor for detached `codex exec --json` tasks."""

from __future__ import annotations

import argparse
import errno
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

VERSION = "0.6.1"
DEFAULT_STATE_DIR = "~/.codex-agents"
TERMINAL_STATES = {"awaiting_reply", "done", "failed", "killed", "stalled"}
ATTENTION_ORDER = {"awaiting_reply": 0, "failed": 1, "stalled": 2, "working": 3}
TURN_STARTUP_GRACE_S = 15
CDX_EFFORTS = ("medium", "high", "max")
CODEX_DEFAULT_MODEL = "sol"
CODEX_MODEL_ALIASES = {
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
}
FABLE_EFFORTS = {"medium": "low", "high": "medium", "max": "xhigh"}
SPAWN_PREAMBLE = """[orchestration protocol] You are run non-interactively by an orchestrating
agent. If you hit a decision you cannot make yourself (missing access,
ambiguous requirement, a destructive or irreversible choice), do not guess:
end your turn with a line starting exactly `QUESTION: ` followed by what you
need to know. You will receive the answer as a follow-up message in this
same session. Otherwise end with a normal final summary of what you did and
how you verified it.
"""
SEND_PREAMBLE = "[orchestration protocol] Same rules as before: escalate with QUESTION: if blocked.\n"
ADJECTIVES = ["brisk", "calm", "clear", "nimble", "plain", "quick", "steady", "tidy"]
NOUNS = ["anchor", "beacon", "delta", "field", "harbor", "otter", "signal", "stone"]


class CdxError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CodexBackend:
    """`codex exec --json`: prompt via stdin, thread via thread.started events."""

    name = "codex"
    bin_name, bin_env, install_hint = "codex", "CDX_CODEX_BIN", "install Codex CLI"
    efforts = {"medium": "medium", "high": "high", "max": "xhigh"}
    uses_prompt_file = False  # prompt is piped on stdin

    def build_cmd(self, meta: dict[str, Any], prompt_file: Path, mode: str, backend_bin: str) -> list[str]:
        model = meta.get("model")
        effort = resolved_provider_effort(meta)
        if mode == "spawn":
            repo = Path(str(meta["repo"]))
            cmd = [backend_bin, "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "-C", str(repo)]
            if not is_git_repo(repo):
                cmd.append("--skip-git-repo-check")
            if model:
                cmd += ["-m", str(model)]
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            cmd.append("-")
            return cmd
        thread_id = meta.get("thread_id")
        if not thread_id:
            raise CdxError(4, "task has no thread_id yet; wait for status or spawn a new task")
        cmd = [backend_bin, "exec", "resume", "--dangerously-bypass-approvals-and-sandbox", "--json"]
        if model:
            cmd += ["-m", str(model)]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        cmd += [str(thread_id), "-"]
        return cmd

    def run_cwd(self, meta: dict[str, Any], mode: str) -> str | None:
        # spawn gets -C <repo>; resume has no -C, so it needs the process cwd
        return meta.get("repo") if mode == "resume" else None

    def thread_id(self, events: list[dict[str, Any]]) -> str | None:
        for event in reversed(events):
            thread_id = event.get("thread_id")
            if event.get("type") == "thread.started" and isinstance(thread_id, str):
                return thread_id
        return None

    def turn_count(self, events: list[dict[str, Any]]) -> int:
        return sum(1 for event in events if event.get("type") == "turn.completed")

    def failed(self, events: list[dict[str, Any]]) -> bool:
        return has_event(events, "turn.failed", "thread.error")

    def last_agent_message(self, events: list[dict[str, Any]]) -> str | None:
        for event in reversed(events):
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    return text
            if event.get("type") == "agent_message" and isinstance(event.get("text"), str):
                return event["text"]
        return None

    def last_activity(self, event: dict[str, Any]) -> str | None:
        typ = event.get("type")
        item = event.get("item")
        if isinstance(item, dict):
            itype = item.get("type")
            if itype == "agent_message" and isinstance(item.get("text"), str):
                return f"msg: {condense(item['text'])}"
            if "command" in item:
                return f"command: {condense(str(item['command']))}"
            if "path" in item:
                return f"file: {condense(str(item['path']))}"
        if typ:
            return str(typ)
        return None

    def summarize_event(self, event: dict[str, Any]) -> str | None:
        typ = str(event.get("type") or "event")
        item = event.get("item")
        if isinstance(item, dict):
            itype = str(item.get("type") or "item")
            if itype == "agent_message" and isinstance(item.get("text"), str):
                return f"msg {condense(item['text'], 120)}"
            if "command" in item:
                text = f"command {item['command']}"
                if "exit_code" in item:
                    text += f" exit={item['exit_code']}"
                return condense(text, 160)
            if "path" in item:
                return f"file {item['path']}"
            return condense(f"{itype} {json.dumps(item, separators=(',', ':'))}", 160)
        if typ == "turn.completed":
            return "turn.completed"
        if typ in {"turn.failed", "thread.error"}:
            return condense(f"{typ} {event.get('message') or event.get('error') or ''}", 160)
        return typ

    def thinking_tail(self, tdir: Path, events: list[dict[str, Any]], chars: int) -> str:
        # codex streams reasoning on stderr; tail the raw log
        path = tdir / "stderr.log"
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - chars * 4))
            return ansi_strip(handle.read().decode("utf-8", errors="replace"))[-chars:]


class ClaudeBackend:
    """`claude -p --output-format stream-json`: prompt via stdin, session_id events."""

    name = "claude"
    bin_name, bin_env, install_hint = "claude", "CDX_CLAUDE_BIN", "install Claude Code"
    efforts = {"medium": "medium", "high": "high", "max": "xhigh"}
    uses_prompt_file = False  # prompt is piped on stdin

    def build_cmd(self, meta: dict[str, Any], prompt_file: Path, mode: str, backend_bin: str) -> list[str]:
        model = meta.get("model")
        effort = resolved_provider_effort(meta)
        if mode == "spawn":
            cmd = [backend_bin, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--dangerously-skip-permissions"]
        else:
            thread_id = meta.get("thread_id")
            if not thread_id:
                raise CdxError(4, "task has no thread_id yet; wait for status or spawn a new task")
            cmd = [
                backend_bin,
                "-p",
                "--resume",
                str(thread_id),
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--dangerously-skip-permissions",
            ]
        if model:
            cmd += ["--model", str(model)]
        if effort:
            cmd += ["--effort", str(effort)]
        return cmd

    def run_cwd(self, meta: dict[str, Any], mode: str) -> str | None:
        return meta.get("repo")

    def thread_id(self, events: list[dict[str, Any]]) -> str | None:
        for event in events:
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

    def turn_count(self, events: list[dict[str, Any]]) -> int:
        return sum(1 for event in events if event.get("type") == "result")

    def failed(self, events: list[dict[str, Any]]) -> bool:
        for event in events:
            subtype = event.get("subtype")
            if event.get("type") == "result":
                if event.get("is_error") is True:
                    return True
                if isinstance(subtype, str) and "error" in subtype.lower():
                    return True
        return False

    def last_agent_message(self, events: list[dict[str, Any]]) -> str | None:
        for event in reversed(events):
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                return event["result"]
        for event in reversed(events):
            if event.get("type") == "assistant":
                text = text_from_claude_message(event)
                if text is not None:
                    return text
        return None

    def last_activity(self, event: dict[str, Any]) -> str | None:
        return summarize_claude_event(event)

    def summarize_event(self, event: dict[str, Any]) -> str | None:
        return summarize_claude_event(event)

    def thinking_tail(self, tdir: Path, events: list[dict[str, Any]], chars: int) -> str:
        return claude_thinking_tail(events, chars)


class GrokBackend:
    """xAI Grok Build CLI: prompt via --prompt-file, streaming-json events.

    The stream carries only thought/text deltas plus a terminal `end` event
    (with sessionId) or an `error` event. Tool calls are NOT surfaced, so peek
    and last_activity are sparse for grok; stall detection still works because
    thought/text deltas keep bytes flowing.
    """

    name = "grok"
    bin_name, bin_env, install_hint = "grok", "CDX_GROK_BIN", "install Grok CLI"
    # grok's --reasoning-effort accepts any string unvalidated; assume low/medium/high.
    # grok is cheap, so medium/high map straight through instead of one tier down
    efforts = {"medium": "medium", "high": "high", "max": "high"}
    uses_prompt_file = True  # prompt is passed as --prompt-file, not stdin

    def build_cmd(self, meta: dict[str, Any], prompt_file: Path, mode: str, backend_bin: str) -> list[str]:
        model = meta.get("model")
        effort = resolved_provider_effort(meta)
        if mode == "spawn":
            cmd = [backend_bin, "--prompt-file", str(prompt_file), "--output-format", "streaming-json", "--permission-mode", "bypassPermissions"]
        else:
            thread_id = meta.get("thread_id")
            if not thread_id:
                raise CdxError(4, "task has no thread_id yet; wait for status or spawn a new task")
            cmd = [backend_bin, "--resume", str(thread_id), "--prompt-file", str(prompt_file), "--output-format", "streaming-json", "--permission-mode", "bypassPermissions"]
        if model:
            cmd += ["-m", str(model)]
        if effort:
            cmd += ["--reasoning-effort", str(effort)]
        return cmd

    def run_cwd(self, meta: dict[str, Any], mode: str) -> str | None:
        return meta.get("repo")

    def thread_id(self, events: list[dict[str, Any]]) -> str | None:
        # sessionId only appears on the terminal `end` event; None mid-run is expected
        for event in reversed(events):
            if event.get("type") == "end":
                session_id = event.get("sessionId")
                if isinstance(session_id, str) and session_id:
                    return session_id
        return None

    def turn_count(self, events: list[dict[str, Any]]) -> int:
        return sum(1 for event in events if event.get("type") == "end")

    def failed(self, events: list[dict[str, Any]]) -> bool:
        return has_event(events, "error")

    def last_agent_message(self, events: list[dict[str, Any]]) -> str | None:
        # text deltas of the last completed turn: those between the 2nd-to-last and last `end`
        end_indices = [index for index, event in enumerate(events) if event.get("type") == "end"]
        if not end_indices:
            return None
        last_end = end_indices[-1]
        prev_end = end_indices[-2] if len(end_indices) >= 2 else -1
        parts = [
            event["data"]
            for event in events[prev_end + 1 : last_end]
            if event.get("type") == "text" and isinstance(event.get("data"), str)
        ]
        return "".join(parts) if parts else None

    def last_activity(self, event: dict[str, Any]) -> str | None:
        return self.summarize_event(event)

    def summarize_event(self, event: dict[str, Any]) -> str | None:
        typ = event.get("type")
        if typ in {"thought", "text"}:
            return None  # per-delta events are too granular to summarize
        if typ == "end":
            return f"end.{event.get('stopReason') or 'EndTurn'}"
        if typ == "error":
            return condense(f"error {event.get('message') or ''}", 160)
        return str(typ) if typ else None

    def thinking_tail(self, tdir: Path, events: list[dict[str, Any]], chars: int) -> str:
        pieces: list[str] = []
        total = 0
        for event in reversed(events):
            if event.get("type") != "thought":
                continue
            data = event.get("data")
            if not isinstance(data, str) or not data:
                continue
            pieces.append(data)
            total += len(data)
            if total >= chars:
                break
        return "".join(reversed(pieces))[-chars:]


BACKENDS = {"codex": CodexBackend(), "claude": ClaudeBackend(), "grok": GrokBackend()}
CONFIG_KEYS = {f"model.{name}" for name in BACKENDS}


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def json_out(data: Any) -> None:
    sys.stdout.write(json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n")


def now() -> float:
    return time.time()


def state_root(args: argparse.Namespace) -> Path:
    value = getattr(args, "state_dir", None) or os.environ.get("CDX_STATE_DIR") or DEFAULT_STATE_DIR
    return Path(value).expanduser().resolve()


def tasks_dir(root: Path) -> Path:
    return root / "tasks"


def config_path(root: Path) -> Path:
    return root / "config.json"


def task_dir(root: Path, task: str) -> Path:
    return tasks_dir(root) / task


def ensure_state(root: Path) -> None:
    tasks_dir(root).mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, data: dict[str, Any], create_parent: bool = True) -> None:
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_meta(tdir: Path) -> dict[str, Any]:
    return load_json(tdir / "meta.json", {})


def save_meta(tdir: Path, meta: dict[str, Any]) -> None:
    atomic_write_json(tdir / "meta.json", meta)


def finalize_meta(tdir: Path, meta: dict[str, Any]) -> None:
    """Persist meta from the detached supervisor without resurrecting a cleaned task.

    The supervisor writes final state after the backend process exits. If `clean`
    removed the task dir in the meantime, a plain `save_meta` would re-create it
    (its parent `mkdir`), resurrecting the task as a partial `failed` entry. Here
    we skip the write when the dir is gone and, to close the check-then-write race,
    write without re-creating the parent so a concurrent `rmtree` surfaces as
    FileNotFoundError instead of a resurrected directory."""
    if not tdir.exists():
        return
    try:
        atomic_write_json(tdir / "meta.json", meta, create_parent=False)
    except FileNotFoundError:
        return


def valid_task_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", name))


def generate_name(root: Path) -> str:
    for _ in range(200):
        name = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
        if not task_dir(root, name).exists():
            return name
    raise CdxError(5, "could not generate a free task name; pass --name explicitly")


def locate_binary(name: str, env_var: str, install_hint: str) -> str:
    override = os.environ.get(env_var)
    if override:
        path = Path(override).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise CdxError(7, f"{name} binary not executable at {env_var}={override}; set {env_var} or {install_hint}")
    found = shutil.which(name)
    if found:
        return found
    raise CdxError(7, f"{name} binary not found; {install_hint} or set {env_var}")


def locate_backend(backend: str) -> str:
    adapter = BACKENDS.get(backend) or BACKENDS["codex"]
    return locate_binary(adapter.bin_name, adapter.bin_env, adapter.install_hint)


def meta_backend(meta: dict[str, Any]) -> str:
    value = meta.get("backend")
    return str(value) if value in BACKENDS else "codex"


def task_owner() -> str:
    """Identity that scopes `clean` to the session/worktree that spawned a task.

    The registry (`~/.codex-agents/tasks`) is shared by every session on the
    machine, so a blanket `clean --terminal`/`--all` would delete sibling
    sessions' still-uncollected results. Stamping an owner and scoping clean to
    it keeps parallel sessions from stepping on each other.

    `CDX_OWNER` (the skill sets it to a stable session id) wins; otherwise the
    resolved cwd, so parallel *worktrees* get distinct owners with zero config.
    Two sessions sharing one cwd should set `CDX_OWNER` to stay isolated."""
    override = os.environ.get("CDX_OWNER", "").strip()
    if override:
        return override
    return str(Path.cwd().resolve())


def validate_effort(effort: str | None) -> None:
    if not effort:
        return
    if effort not in CDX_EFFORTS:
        raise CdxError(2, f"invalid --effort {effort!r}; valid choices: {', '.join(CDX_EFFORTS)}")


def backend_effort(backend: str, effort: str | None) -> str | None:
    if not effort:
        return None
    validate_effort(effort)
    return BACKENDS[backend].efforts[effort]


def is_fable_model(model: str | None) -> bool:
    if not model:
        return False
    value = model.lower()
    return value == "fable" or value == "claude-fable-5" or value.startswith("claude-fable-5-")


def resolve_execution(backend: str, effort: str, model: str | None) -> tuple[str | None, str]:
    """Resolve cdx's stable model/effort vocabulary to a concrete provider execution."""
    validate_effort(effort)
    if backend == "codex":
        alias = (model or CODEX_DEFAULT_MODEL).lower()
        model = CODEX_MODEL_ALIASES.get(alias, model)
    if backend == "claude" and is_fable_model(model):
        return model, FABLE_EFFORTS[effort]
    provider_effort = backend_effort(backend, effort)
    assert provider_effort is not None
    return model, provider_effort


def resolved_provider_effort(meta: dict[str, Any]) -> str | None:
    value = meta.get("provider_effort")
    if isinstance(value, str) and value:
        return value
    backend = meta_backend(meta)
    effort = meta.get("effort")
    if not isinstance(effort, str):
        return None
    # Legacy tasks predate stored provider resolution. Keep their original
    # backend-only translation on resume instead of silently changing model
    # semantics after a skill update.
    return backend_effort(backend, effort)


def execution_fields(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": meta.get("model"),
        "effort": meta.get("effort"),
        "provider_effort": resolved_provider_effort(meta),
    }


def load_config(root: Path) -> dict[str, Any]:
    path = config_path(root)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise CdxError(5, f"config file must contain a JSON object: {path}")
    return data


def save_config(root: Path, data: dict[str, Any]) -> None:
    atomic_write_json(config_path(root), data)


def require_config_key(key: str) -> None:
    if key not in CONFIG_KEYS:
        raise CdxError(2, f"unsupported config key {key}; valid keys: {', '.join(sorted(CONFIG_KEYS))}")


def get_config_key(data: dict[str, Any], key: str) -> str | None:
    require_config_key(key)
    section, name = key.split(".", 1)
    section_value = data.get(section)
    if isinstance(section_value, dict) and isinstance(section_value.get(name), str):
        return section_value[name]
    return None


def set_config_key(data: dict[str, Any], key: str, value: str) -> dict[str, Any]:
    require_config_key(key)
    section, name = key.split(".", 1)
    next_data = dict(data)
    section_value = next_data.get(section)
    if not isinstance(section_value, dict):
        section_value = {}
    else:
        section_value = dict(section_value)
    section_value[name] = value
    next_data[section] = section_value
    return next_data


def unset_config_key(data: dict[str, Any], key: str) -> dict[str, Any]:
    require_config_key(key)
    section, name = key.split(".", 1)
    next_data = dict(data)
    section_value = next_data.get(section)
    if isinstance(section_value, dict):
        section_value = dict(section_value)
        section_value.pop(name, None)
        if section_value:
            next_data[section] = section_value
        else:
            next_data.pop(section, None)
    return next_data


def configured_model(root: Path, backend: str) -> str | None:
    return get_config_key(load_config(root), f"model.{backend}")


def read_prompt(args: argparse.Namespace) -> str:
    files: list[str] = getattr(args, "file", None) or []
    has_positional = getattr(args, "prompt", None) is not None
    if has_positional == bool(files):
        raise CdxError(2, "provide exactly one prompt source: PROMPT, or one or more -f FILE (repeatable; - for stdin)")
    if files:
        if files.count("-") > 1:
            raise CdxError(2, "stdin (-) can appear at most once among -f arguments")
        parts = []
        for entry in files:
            if entry == "-":
                parts.append(sys.stdin.read())
            else:
                parts.append(Path(entry).expanduser().read_text(encoding="utf-8"))
        return "\n\n".join(part.rstrip("\n") for part in parts) + "\n"
    if args.prompt == "-":
        return sys.stdin.read()
    return args.prompt


def with_preamble(prompt: str, preamble: str, no_preamble: bool) -> str:
    if no_preamble:
        return prompt
    return f"{preamble.rstrip()}\n\n{prompt}"


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:
        result = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2)
        if result.returncode == 0 and result.stdout.strip().startswith("Z"):
            return False
    except (OSError, subprocess.SubprocessError):
        pass
    return True


def combined_size(tdir: Path) -> int:
    total = 0
    for name in ("events.jsonl", "stderr.log"):
        path = tdir / name
        if path.exists():
            total += path.stat().st_size
    return total


def output_mtime(tdir: Path) -> float | None:
    mtimes = []
    for name in ("events.jsonl", "stderr.log"):
        path = tdir / name
        if path.exists() and path.stat().st_size:
            mtimes.append(path.stat().st_mtime)
    return max(mtimes) if mtimes else None


def read_events(tdir: Path) -> list[dict[str, Any]]:
    events_path = tdir / "events.jsonl"
    events: list[dict[str, Any]] = []
    if not events_path.exists():
        return events
    with events_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def newest_thread_id(events: list[dict[str, Any]], backend: str = "codex") -> str | None:
    return BACKENDS.get(backend, BACKENDS["codex"]).thread_id(events)


def text_from_claude_message(event: dict[str, Any]) -> str | None:
    text = event.get("text")
    if isinstance(text, str):
        return text
    content = event.get("content")
    if isinstance(content, str):
        return content
    message = event.get("message")
    if isinstance(message, dict):
        nested_text = message.get("text")
        if isinstance(nested_text, str):
            return nested_text
        content = message.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        if parts:
            return "".join(parts)
    return None


def last_agent_message(events: list[dict[str, Any]], backend: str = "codex") -> str | None:
    return BACKENDS.get(backend, BACKENDS["codex"]).last_agent_message(events)


def extract_question(message: str | None) -> str | None:
    if not message:
        return None
    lines = message.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("QUESTION:"):
            first = line[len("QUESTION:") :]
            if first.startswith(" "):
                first = first[1:]
            rest = [first] + lines[index + 1 :]
            return "\n".join(rest).strip() or ""
    return None


def turn_count(events: list[dict[str, Any]], backend: str = "codex") -> int:
    return BACKENDS.get(backend, BACKENDS["codex"]).turn_count(events)


def turns_launched(meta: dict[str, Any], events: list[dict[str, Any]] | None = None) -> int:
    value = meta.get("turns_launched")
    if isinstance(value, int) and value > 0:
        return value
    legacy_turns = int(meta.get("turns") or 0)
    completed = turn_count(events or [], meta_backend(meta))
    return max(1, legacy_turns, completed)


def latest_usage(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        usage = event.get("usage")
        if isinstance(usage, dict):
            return usage
    return None


def has_event(events: list[dict[str, Any]], *types: str) -> bool:
    wanted = set(types)
    return any(event.get("type") in wanted for event in events)


def derive_state(meta: dict[str, Any], events: list[dict[str, Any]], alive: bool) -> str:
    if meta.get("state") in {"killed", "stalled"}:
        return str(meta["state"])

    backend = meta_backend(meta)
    adapter = BACKENDS[backend]
    launched = turns_launched(meta, events)
    completed = adapter.turn_count(events)
    if completed < launched:
        launched_at = float(meta.get("turn_launched_at") or meta.get("spawned_at") or 0)
        if alive or now() - launched_at < TURN_STARTUP_GRACE_S:
            return "working"
        return "failed"

    if adapter.failed(events):
        return "failed"
    if completed >= launched and completed > 0:
        return "awaiting_reply" if extract_question(adapter.last_agent_message(events)) is not None else "done"
    if alive:
        return "working"
    if events:
        return "failed"
    if meta.get("state") == "working":
        return "failed"
    return str(meta.get("state") or "failed")


def refresh_meta(tdir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str, bool]:
    meta = load_meta(tdir)
    events = read_events(tdir)
    changed = False
    if meta.get("backend") not in BACKENDS:
        meta["backend"] = "codex"
        changed = True
    backend = meta_backend(meta)
    if not meta.get("turns_launched"):
        meta["turns_launched"] = turns_launched(meta, events)
        changed = True
    if not meta.get("turn_launched_at"):
        meta["turn_launched_at"] = meta.get("spawned_at") or now()
        changed = True
    thread_id = newest_thread_id(events, backend)
    if thread_id and meta.get("thread_id") != thread_id:
        meta["thread_id"] = thread_id
        changed = True
    alive = pid_alive(meta.get("pid"))
    state = derive_state(meta, events, alive)
    if meta.get("state") != state:
        meta["state"] = state
        changed = True
    if int(meta.get("turns") or 0) < int(meta.get("turns_launched") or 0):
        meta["turns"] = int(meta["turns_launched"])
        changed = True
    if changed:
        # read paths (status/list/result/peek) persist derived state opportunistically;
        # use the non-creating write so a concurrent `clean` isn't undone by resurrecting
        # the task dir we just observed
        finalize_meta(tdir, meta)
    return meta, events, state, alive


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def resolve_task(root: Path, name: str) -> tuple[str, Path]:
    exact = task_dir(root, name)
    if exact.exists():
        return name, exact
    names = [path.name for path in tasks_dir(root).glob("*") if path.is_dir()] if tasks_dir(root).exists() else []
    if not names:
        raise CdxError(3, f"unknown task {name}; run cdx list --all to see tasks")
    closest = min(names, key=lambda candidate: edit_distance(name, candidate))
    distance = edit_distance(name, closest)
    if distance <= 2:
        eprint(f"note: task {name} not found; using near match {closest}")
        return closest, task_dir(root, closest)
    raise CdxError(3, f"unknown task {name}; did you mean {closest}?")


def status_payload(name: str, tdir: Path) -> dict[str, Any]:
    meta, events, state, alive = refresh_meta(tdir)
    backend = meta_backend(meta)
    spawned = float(meta.get("spawned_at") or now())
    mtime = output_mtime(tdir)
    last_msg = last_agent_message(events, backend)
    payload = {
        "task": name,
        "backend": backend,
        **execution_fields(meta),
        "state": state,
        "repo": meta.get("repo"),
        "age_s": max(0, int(now() - spawned)),
        "last_output_age_s": None if mtime is None else max(0, int(now() - mtime)),
        "last_activity": last_activity(events, backend),
        "question": extract_question(last_msg),
    }
    payload.update(
        {
            "owner": meta.get("owner"),
            "thread_id": meta.get("thread_id"),
            "pid": meta.get("pid"),
            "pid_alive": alive,
            "turns": int(meta.get("turns") or turn_count(events, backend)),
            "turns_launched": int(meta.get("turns_launched") or turns_launched(meta, events)),
            "turn_launched_at": meta.get("turn_launched_at"),
            "spawned_at": meta.get("spawned_at"),
            "events_total": len(events),
            "output_bytes": combined_size(tdir),
            "usage": latest_usage(events),
        }
    )
    if "stall_reason" in meta:
        payload["stall_reason"] = meta["stall_reason"]
    return payload


def list_payload(name: str, tdir: Path) -> dict[str, Any]:
    detail = status_payload(name, tdir)
    return {
        key: detail[key]
        for key in (
            "task",
            "backend",
            "model",
            "effort",
            "provider_effort",
            "state",
            "owner",
            "repo",
            "age_s",
            "last_output_age_s",
            "last_activity",
            "question",
        )
    }


def condense(text: str, limit: int = 80) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


def last_activity(events: list[dict[str, Any]], backend: str = "codex") -> str | None:
    adapter = BACKENDS.get(backend, BACKENDS["codex"])
    for event in reversed(events):
        summary = adapter.last_activity(event)
        if summary:
            return summary
    return None


def is_git_repo(path: Path) -> bool:
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def backend_cmd(meta: dict[str, Any], prompt_file: Path, mode: str, backend_bin: str) -> list[str]:
    backend = meta_backend(meta)
    return BACKENDS[backend].build_cmd(meta, prompt_file, mode, backend_bin)


def interrupt_pid(pid: int | None) -> None:
    if not pid_alive(pid):
        return
    try:
        os.kill(int(pid), signal.SIGINT)
    except OSError:
        return
    deadline = now() + 10
    while now() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(int(pid), signal.SIGKILL)
    except OSError:
        pass


def launch_helper(root: Path, name: str, prompt_path: Path, mode: str, stall_after: int, backend: str, backend_bin: str) -> int:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "__run_turn",
        "--state-dir",
        str(root),
        "--task",
        name,
        "--prompt-file",
        str(prompt_path),
        "--mode",
        mode,
        "--stall-after",
        str(stall_after),
        "--backend",
        backend,
        "--backend-bin",
        backend_bin,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = now() + 5
    tdir = task_dir(root, name)
    while now() < deadline:
        meta = load_meta(tdir)
        pid = meta.get("pid")
        if isinstance(pid, int) and pid > 0:
            return pid
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    return proc.pid


def spawn_task(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    backend = args.backend
    validate_effort(args.effort)
    requested_model = args.model if args.model is not None else configured_model(root, backend)
    model, provider_effort = resolve_execution(backend, args.effort, requested_model)
    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists() or not repo.is_dir():
        raise CdxError(2, f"repo does not exist or is not a directory: {repo}; pass a valid -C/--repo")
    name = args.name or generate_name(root)
    if not valid_task_name(name):
        raise CdxError(2, "task name must use lowercase letters, digits, and hyphens")
    tdir = task_dir(root, name)
    if tdir.exists():
        raise CdxError(4, f"task {name} already exists; choose --name or use cdx send {name}")
    prompt = with_preamble(read_prompt(args), SPAWN_PREAMBLE, args.no_preamble)
    tdir.mkdir(parents=True)
    (tdir / "turns").mkdir()
    (tdir / "prompt.md").write_text(prompt, encoding="utf-8")
    for other in tasks_dir(root).glob("*"):
        if other == tdir or not other.is_dir():
            continue
        item = list_payload(other.name, other)
        if item["repo"] == str(repo) and item["state"] not in TERMINAL_STATES:
            eprint(f"warning: task {other.name} is already non-terminal in repo {repo}")
    meta = {
        "task": name,
        "backend": backend,
        "owner": task_owner(),
        "repo": str(repo),
        "thread_id": None,
        "pid": None,
        "spawned_at": now(),
        "model": model,
        "effort": args.effort,
        "provider_effort": provider_effort,
        "state": "working",
        "turns": 1,
        "turns_launched": 1,
        "turn_launched_at": None,
        "last_exit_code": None,
    }
    meta["turn_launched_at"] = meta["spawned_at"]
    save_meta(tdir, meta)
    backend_bin = locate_backend(backend)
    pid = launch_helper(root, name, tdir / "prompt.md", "spawn", args.stall_after, backend, backend_bin)
    meta = load_meta(tdir)
    if not meta.get("pid"):
        meta["pid"] = pid
        save_meta(tdir, meta)
    payload = {
        "task": name,
        "backend": backend,
        "repo": str(repo),
        "pid": meta.get("pid"),
        "state": "working",
        **execution_fields(meta),
    }
    emit(args, payload, f"{name} working pid={payload['pid']} repo={repo}")
    return 0


def run_turn(args: argparse.Namespace) -> int:
    root = Path(args.state_dir).expanduser().resolve()
    tdir = task_dir(root, args.task)
    meta = load_meta(tdir)
    backend = args.backend or meta_backend(meta)
    meta["backend"] = backend
    mode = args.mode
    adapter = BACKENDS[backend]
    prompt_file = Path(args.prompt_file)
    cmd = adapter.build_cmd(meta, prompt_file, mode, args.backend_bin)
    out_mode = "wb" if mode == "spawn" else "ab"
    # grok reads the prompt from --prompt-file, not stdin; other backends pipe it in
    stdin_file = None if adapter.uses_prompt_file else prompt_file.open("rb")
    with (tdir / "events.jsonl").open(out_mode) as stdout, (tdir / "stderr.log").open(out_mode) as stderr:
        cwd = adapter.run_cwd(meta, mode)
        proc = subprocess.Popen(cmd, stdin=stdin_file or subprocess.DEVNULL, stdout=stdout, stderr=stderr, cwd=cwd, start_new_session=True)
        meta["pid"] = proc.pid
        meta["state"] = "working"
        if not meta.get("turns_launched"):
            meta["turns_launched"] = turns_launched(meta)
        if not meta.get("turn_launched_at"):
            meta["turn_launched_at"] = now()
        if int(meta.get("turns") or 0) < int(meta.get("turns_launched") or 0):
            meta["turns"] = int(meta["turns_launched"])
        save_meta(tdir, meta)
        stall_after = int(args.stall_after)
        last_size = combined_size(tdir)
        last_growth = now()
        check_interval = 30
        next_check = now() + check_interval
        thread_id_captured = bool(meta.get("thread_id"))
        while proc.poll() is None:
            time.sleep(0.2)
            if not thread_id_captured:
                events = read_events(tdir)
                thread_id = newest_thread_id(events, backend)
                if thread_id and meta.get("thread_id") != thread_id:
                    meta["thread_id"] = thread_id
                    finalize_meta(tdir, meta)
                    thread_id_captured = True
            if not stall_after or now() < next_check:
                continue
            size = combined_size(tdir)
            if size > last_size:
                last_size = size
                last_growth = now()
            elif now() - last_growth >= stall_after:
                before = last_size
                try:
                    proc.send_signal(signal.SIGINT)
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                after = combined_size(tdir)
                meta.update(
                    {
                        "state": "stalled",
                        "last_exit_code": proc.returncode,
                        "stall_reason": f"no byte growth for {stall_after}s",
                        "stall_bytes_before": before,
                        "stall_bytes_after": after,
                    }
                )
                finalize_meta(tdir, meta)
                return 0
            next_check = now() + check_interval
    if stdin_file is not None:
        stdin_file.close()
    events = read_events(tdir)
    meta = load_meta(tdir)
    backend = meta_backend(meta)
    thread_id = newest_thread_id(events, backend)
    if thread_id:
        meta["thread_id"] = thread_id
    meta["last_exit_code"] = proc.returncode
    meta["turns_launched"] = turns_launched(meta, events)
    meta["turns"] = max(int(meta.get("turns") or 0), int(meta["turns_launched"]), turn_count(events, backend))
    meta["state"] = derive_state(meta, events, False)
    finalize_meta(tdir, meta)
    return 0


def emit(args: argparse.Namespace, data: Any, human: str) -> None:
    if getattr(args, "json", False):
        json_out(data)
    else:
        print(human)


def list_tasks(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    rows = []
    cutoff = now() - 24 * 60 * 60
    for path in tasks_dir(root).glob("*"):
        if not path.is_dir():
            continue
        row = list_payload(path.name, path)
        meta = load_meta(path)
        terminal = row["state"] in TERMINAL_STATES
        if args.all or not terminal or float(meta.get("spawned_at") or 0) >= cutoff:
            rows.append(row)
    rows.sort(key=lambda row: (ATTENTION_ORDER.get(row["state"], 4), row["task"]))
    if args.json:
        json_out(rows)
    else:
        for row in rows:
            q = f" question={condense(row['question'])}" if row.get("question") else ""
            print(f"{row['task']} {row['state']} {row['repo']}{q}")
    return 0


def status_task(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    name, tdir = resolve_task(root, args.task)
    payload = status_payload(name, tdir)
    emit(args, payload, f"{payload['task']} {payload['state']} pid_alive={payload['pid_alive']} repo={payload['repo']}")
    return state_exit(payload["state"])


def state_exit(state: str) -> int:
    if state == "done":
        return 0
    if state == "working":
        return 10
    if state == "awaiting_reply":
        return 11
    if state == "stalled":
        return 12
    return 13


def summarize_claude_event(event: dict[str, Any]) -> str | None:
    typ = str(event.get("type") or "event")
    if typ == "stream_event":
        return None
    if typ in {"assistant", "user", "system"}:
        text = text_from_claude_message(event)
        if text:
            return f"{typ} {condense(text, 120)}"
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else event.get("content")
        if isinstance(content, list):
            kinds = []
            for part in content:
                if isinstance(part, dict):
                    kinds.append(str(part.get("type") or "item"))
            if kinds:
                return condense(f"{typ} {' '.join(kinds)}", 160)
        return typ
    if typ == "result":
        subtype = str(event.get("subtype") or ("error" if event.get("is_error") else "success"))
        result = event.get("result")
        suffix = f" {condense(result, 120)}" if isinstance(result, str) and result else ""
        return f"result.{subtype}{suffix}"
    return typ


def summarize_event(event: dict[str, Any], backend: str = "codex") -> str | None:
    return BACKENDS.get(backend, BACKENDS["codex"]).summarize_event(event)


def ansi_strip(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


def claude_stream_delta_text(event: dict[str, Any]) -> str:
    if event.get("type") != "stream_event":
        return ""
    candidates: list[Any] = [event]
    nested = event.get("event")
    if isinstance(nested, dict):
        candidates.append(nested)
    for candidate in list(candidates):
        if isinstance(candidate, dict) and isinstance(candidate.get("delta"), dict):
            candidates.append(candidate["delta"])
    parts: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("text", "partial_json"):
            value = candidate.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def claude_thinking_tail(events: list[dict[str, Any]], chars: int) -> str:
    pieces: list[str] = []
    total = 0
    for event in reversed(events):
        delta = claude_stream_delta_text(event)
        if not delta:
            continue
        pieces.append(delta)
        total += len(delta)
        if total >= chars:
            break
    return "".join(reversed(pieces))[-chars:]


def peek_task(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    name, tdir = resolve_task(root, args.task)
    meta = load_meta(tdir)
    backend = meta_backend(meta)
    events = read_events(tdir)
    if args.full:
        message = last_agent_message(events, backend) or ""
        data: Any = {"task": name, "message": message}
        emit(args, data, message)
        return 0
    lines = []
    for event in reversed(events):
        line = summarize_event(event, backend)
        if line:
            lines.append(line)
        if len(lines) >= args.tail:
            break
    lines.reverse()
    thinking = None
    if args.thinking is not None:
        chars = min(int(args.thinking), 1500)
        thinking = BACKENDS.get(backend, BACKENDS["codex"]).thinking_tail(tdir, events, chars)
    if args.json:
        json_out({"task": name, "backend": backend, "items": lines, "thinking": thinking})
    else:
        for line in lines:
            print(line)
        if thinking is not None:
            print(thinking)
    return 0


def result_task(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    name, tdir = resolve_task(root, args.task)
    start = now()
    timeout = int(args.timeout)
    while True:
        payload = status_payload(name, tdir)
        state = payload["state"]
        if state != "working" or not args.wait:
            break
        if now() - start >= timeout:
            raise CdxError(6, f"timeout after {timeout}s waiting for {name}; task keeps running, use cdx peek {name}")
        time.sleep(1)
    if payload["state"] == "working":
        eprint("still working; use --wait or peek")
        return 10
    events = read_events(tdir)
    meta = load_meta(tdir)
    backend = meta_backend(meta)
    message = last_agent_message(events, backend) or ""
    data = {
        "task": name,
        "backend": backend,
        **execution_fields(meta),
        "state": payload["state"],
        "message": message,
        "question": extract_question(message),
        "turns": payload["turns"],
        "duration_s": payload["age_s"],
    }
    emit(args, data, message)
    return 11 if payload["state"] == "awaiting_reply" else (0 if payload["state"] == "done" else 13)


def send_task(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    name, tdir = resolve_task(root, args.task)
    payload = status_payload(name, tdir)
    if payload["state"] == "working" and not args.now:
        raise CdxError(4, "task is running; use --now to interrupt-and-redirect, or wait for result")
    if payload["state"] in {"working", "stalled"}:
        # stalled means the watchdog already killed the process; interrupt is a
        # safety net in case that kill failed, so no --now gate is needed
        interrupt_pid(payload.get("pid"))
    prompt = with_preamble(read_prompt(args), SEND_PREAMBLE, args.no_preamble)
    turns_dir = tdir / "turns"
    turns_dir.mkdir(exist_ok=True)
    meta = load_meta(tdir)
    backend = meta_backend(meta)
    completed = turn_count(read_events(tdir), backend)
    launch_no = int(meta.get("turns_launched") or payload.get("turns_launched") or payload.get("turns") or completed or 1) + 1
    prompt_path = turns_dir / f"{launch_no:04d}.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    meta["state"] = "working"
    meta["turns"] = launch_no
    # a resume after a dead turn (stalled/failed/killed) replaces that turn's
    # slot instead of adding one, or the task could never reach done again
    meta["turns_launched"] = completed + 1
    meta["turn_launched_at"] = now()
    save_meta(tdir, meta)
    backend_bin = locate_backend(backend)
    pid = launch_helper(root, name, prompt_path, "resume", args.stall_after, backend, backend_bin)
    meta = load_meta(tdir)
    data = {
        "task": name,
        "backend": backend,
        "repo": meta.get("repo"),
        "pid": meta.get("pid") or pid,
        "state": "working",
        **execution_fields(meta),
    }
    emit(args, data, f"{name} working pid={data['pid']} repo={data['repo']}")
    return 0


def kill_task(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    name, tdir = resolve_task(root, args.task)
    payload = status_payload(name, tdir)
    if payload["state"] in TERMINAL_STATES:
        eprint(f"task {name} is already terminal; no process killed")
        data = {"task": name, "state": payload["state"]}
        emit(args, data, f"{name} {payload['state']} (already terminal; kill was a no-op)")
        return 0
    interrupt_pid(payload.get("pid"))
    meta = load_meta(tdir)
    meta["state"] = "killed"
    save_meta(tdir, meta)
    data = {"task": name, "state": "killed"}
    emit(args, data, f"{name} killed")
    return 0


def remove_task_dir(path: Path) -> str:
    """Remove a terminal task's dir, tolerating the supervisor's one-shot finalize write.

    As the detached supervisor exits it may write meta.json exactly once. If that lands
    mid-rmtree we see ENOTEMPTY (the file reappears before rmdir) or a vanished-file error
    (rmtree expected a file the finalize temp-cleanup removed). Naively swallowing these
    leaves the directory half-removed. The writer is single-shot, so a few short retries
    converge. Returns "removed", or "finalizing" if it is still racing after the retries
    (very rare, re-run clean to reap). A genuine failure (permissions, I/O) is raised."""
    for _ in range(5):
        try:
            shutil.rmtree(path)
            return "removed"
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY:
                raise CdxError(5, f"could not remove task {path.name}: {exc}")
        if not path.exists():
            return "removed"
        time.sleep(0.02)
    return "removed" if not path.exists() else "finalizing"


def clean_tasks(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    selectors = sum(1 for value in (args.task, args.terminal, args.all) if value)
    if selectors != 1:
        raise CdxError(2, "choose exactly one clean selector: --task NAME, --terminal, or --all")
    # clean only removes tasks that are already terminal. A live (or still-starting)
    # task is never removed: interrupting-then-deleting races the detached supervisor,
    # which can resurrect the directory or orphan the backend. Kill it first instead,
    # the same rule `send` follows. This keeps clean fully race-free by construction.
    selected: list[Path] = []
    skipped_foreign = 0
    skipped_running = 0
    skipped_finalizing = 0
    if args.task:
        # an explicit name is a deliberate target; clean it regardless of owner
        name, tdir = resolve_task(root, args.task)
        if list_payload(name, tdir)["state"] not in TERMINAL_STATES:
            raise CdxError(4, f"task {name} is still running; kill it first (cdx kill {name}), then clean")
        selected.append(tdir)
    else:
        # --terminal/--all sweep the shared registry: scope to our own tasks so a
        # sibling session's uncollected results survive. --any-owner opts back into
        # the global sweep (also the only way to reap pre-owner legacy tasks); -C/--repo
        # is the targeted escape hatch: reap this repo's terminal tasks regardless of the
        # cwd they were spawned from (e.g. tasks spawned with `-C` from another directory).
        me = task_owner()
        repo_filter = str(Path(args.repo).expanduser().resolve()) if args.repo else None
        for path in sorted(tasks_dir(root).glob("*")):
            if not path.is_dir():
                continue
            meta = load_meta(path)
            mine = args.any_owner or meta.get("owner") == me
            repo_match = repo_filter is not None and meta.get("repo") == repo_filter
            if not (mine or repo_match):
                skipped_foreign += 1
                continue
            if list_payload(path.name, path)["state"] not in TERMINAL_STATES:
                # --all's scope includes running tasks but still won't delete them;
                # --terminal never targeted them in the first place
                if args.all:
                    skipped_running += 1
                continue
            selected.append(path)
    names = [path.name for path in selected]
    removed: list[str] = []
    if not args.dry_run:
        for path in selected:
            # re-check right before deleting: a concurrent `send` may have resumed this
            # task since we selected it. Not a full lock, but shrinks the window to ~0.
            if list_payload(path.name, path)["state"] not in TERMINAL_STATES:
                skipped_running += 1
                continue
            if remove_task_dir(path) == "removed":
                removed.append(path.name)
            else:
                # still racing the supervisor's finalize after retries (rare); NOT running,
                # so the fix is to re-run clean, not to kill anything
                skipped_finalizing += 1
    data = {
        "removed": removed,
        "would_remove": names if args.dry_run else [],
        "skipped_foreign": skipped_foreign,
        "skipped_running": skipped_running,
        "skipped_finalizing": skipped_finalizing,
    }
    notes = []
    if skipped_foreign and not args.any_owner:
        notes.append(f"skipped {skipped_foreign} task(s) owned by other sessions; use -C <repo> to reap a specific repo's tasks, or --any-owner for all")
    if skipped_running:
        notes.append(f"skipped {skipped_running} running task(s); kill them first, then clean")
    if skipped_finalizing:
        notes.append(f"{skipped_finalizing} task(s) were finalizing during removal; re-run clean to reap them")
    message = " ".join(names)
    if notes:
        joined = "; ".join(notes)
        message = f"{message} ({joined})" if message else f"({joined})"
    emit(args, data, message)
    return 0


def config_get(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    data = load_config(root)
    if args.json:
        json_out(data)
    else:
        print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def config_set(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    data = set_config_key(load_config(root), args.key, args.value)
    save_config(root, data)
    emit(args, data, f"set {args.key}")
    return 0


def config_unset(args: argparse.Namespace) -> int:
    root = state_root(args)
    ensure_state(root)
    data = unset_config_key(load_config(root), args.key)
    save_config(root, data)
    emit(args, data, f"unset {args.key}")
    return 0


def doctor(args: argparse.Namespace) -> int:
    root = state_root(args)
    checks = []
    codex = BACKENDS["codex"]
    codex_fix = f"{codex.install_hint} or set {codex.bin_env}"
    try:
        codex_bin = locate_backend("codex")
        version = subprocess.run([codex_bin, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        checks.append({"name": "codex", "ok": version.returncode == 0, "detail": (version.stdout or version.stderr).strip(), "fix": codex_fix})
        help_result = subprocess.run([codex_bin, "exec", "--help"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        checks.append({"name": "codex exec --json", "ok": "--json" in help_result.stdout, "detail": "supported" if "--json" in help_result.stdout else "missing --json", "fix": "upgrade Codex CLI"})
    except CdxError as exc:
        checks.append({"name": "codex", "ok": False, "detail": exc.message, "fix": codex_fix})
    # optional backends: a missing binary is a warning, not a failure
    for name in ("claude", "grok"):
        adapter = BACKENDS[name]
        fix = f"{adapter.install_hint} or set {adapter.bin_env}"
        try:
            binpath = locate_binary(adapter.bin_name, adapter.bin_env, adapter.install_hint)
            version = subprocess.run([binpath, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            checks.append(
                {
                    "name": name,
                    "ok": version.returncode == 0,
                    "severity": "pass" if version.returncode == 0 else "warning",
                    "detail": (version.stdout or version.stderr).strip(),
                    "fix": fix,
                }
            )
        except CdxError as exc:
            checks.append(
                {
                    "name": name,
                    "ok": True,
                    "severity": "warning",
                    "detail": exc.message,
                    "fix": fix,
                }
            )
    try:
        ensure_state(root)
        probe = root / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append({"name": "state dir writable", "ok": True, "detail": str(root), "fix": None})
    except OSError as exc:
        checks.append({"name": "state dir writable", "ok": False, "detail": str(exc), "fix": f"make {root} writable"})
    sessions = Path("~/.codex/sessions").expanduser()
    checks.append({"name": "sessions dir", "ok": sessions.exists(), "detail": str(sessions), "fix": "run Codex once to create persisted sessions"})
    orphans = 0
    if tasks_dir(root).exists():
        for path in tasks_dir(root).glob("*"):
            if path.is_dir():
                meta = load_meta(path)
                if meta.get("state") == "working" and not pid_alive(meta.get("pid")):
                    orphans += 1
    checks.append({"name": "orphaned tasks", "ok": True, "detail": str(orphans), "fix": "run cdx status TASK to auto-heal working tasks to failed"})
    if args.json:
        json_out({"checks": checks})
    else:
        for check in checks:
            label = str(check.get("severity") or ("pass" if check["ok"] else "fail"))
            print(f"{label} {check['name']}: {check['detail']}")
            if label != "pass" and check.get("fix"):
                print(f"  fix: {check['fix']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cdx")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state-dir")
    sub = parser.add_subparsers(dest="command", required=True)

    def globals_for(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        sp.add_argument("--state-dir", default=argparse.SUPPRESS)

    spawn = sub.add_parser("spawn")
    globals_for(spawn)
    spawn.add_argument("prompt", nargs="?")
    spawn.add_argument("-f", "--file", action="append")
    spawn.add_argument("-C", "--repo", required=True)
    spawn.add_argument("--backend", choices=list(BACKENDS), default="codex")
    spawn.add_argument("--name")
    spawn.add_argument(
        "--model",
        help="model tier for this task; codex: sol|terra (default sol), claude: opus|sonnet; raw provider model names pass through",
    )
    spawn.add_argument(
        "--effort",
        choices=CDX_EFFORTS,
        default="medium",
        help=(
            "reasoning dial, uniform across backends: medium=medium, high=high, max=xhigh "
            "(grok caps at high; Fable override: medium=low, high=medium, max=xhigh)"
        ),
    )
    spawn.add_argument("--no-preamble", action="store_true")
    spawn.add_argument("--stall-after", type=int, default=300)
    spawn.set_defaults(func=spawn_task)

    listing = sub.add_parser("list")
    globals_for(listing)
    listing.add_argument("--all", action="store_true")
    listing.set_defaults(func=list_tasks)

    status = sub.add_parser("status")
    globals_for(status)
    status.add_argument("task")
    status.set_defaults(func=status_task)

    peek = sub.add_parser("peek")
    globals_for(peek)
    peek.add_argument("task")
    peek.add_argument("--tail", type=int, default=15)
    peek.add_argument("--thinking", nargs="?", const=1000, type=int)
    peek.add_argument("--full", action="store_true")
    peek.set_defaults(func=peek_task)

    result = sub.add_parser("result")
    globals_for(result)
    result.add_argument("task")
    result.add_argument("--wait", action="store_true")
    result.add_argument("--timeout", type=int, default=3600)
    result.set_defaults(func=result_task)

    send = sub.add_parser("send")
    globals_for(send)
    send.add_argument("task")
    send.add_argument("prompt", nargs="?")
    send.add_argument("-f", "--file", action="append")
    send.add_argument("--now", action="store_true")
    send.add_argument("--no-preamble", action="store_true")
    send.add_argument("--stall-after", type=int, default=300)
    send.set_defaults(func=send_task)

    kill = sub.add_parser("kill")
    globals_for(kill)
    kill.add_argument("task")
    kill.set_defaults(func=kill_task)

    clean = sub.add_parser("clean")
    globals_for(clean)
    clean.add_argument("--task")
    clean.add_argument("--terminal", action="store_true")
    clean.add_argument("--all", action="store_true")
    clean.add_argument("--any-owner", action="store_true", help="with --terminal/--all, include tasks owned by other sessions (default: only your own)")
    clean.add_argument("-C", "--repo", help="with --terminal/--all, also reap terminal tasks that target this repo, regardless of the cwd they were spawned from")
    clean.add_argument("--dry-run", action="store_true")
    clean.set_defaults(func=clean_tasks)

    config = sub.add_parser("config")
    globals_for(config)
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_get_parser = config_sub.add_parser("get")
    config_get_parser.set_defaults(func=config_get)
    config_set_parser = config_sub.add_parser("set")
    config_set_parser.add_argument("key")
    config_set_parser.add_argument("value")
    config_set_parser.set_defaults(func=config_set)
    config_unset_parser = config_sub.add_parser("unset")
    config_unset_parser.add_argument("key")
    config_unset_parser.set_defaults(func=config_unset)

    doc = sub.add_parser("doctor")
    globals_for(doc)
    doc.set_defaults(func=doctor)

    helper = sub.add_parser("__run_turn")
    helper.add_argument("--state-dir", required=True)
    helper.add_argument("--task", required=True)
    helper.add_argument("--prompt-file", required=True)
    helper.add_argument("--mode", choices=["spawn", "resume"], required=True)
    helper.add_argument("--stall-after", type=int, required=True)
    helper.add_argument("--backend", choices=list(BACKENDS), required=True)
    helper.add_argument("--backend-bin", required=True)
    helper.set_defaults(func=run_turn)
    return parser


def normalize_global_args(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    commands = {"spawn", "list", "status", "peek", "result", "send", "kill", "clean", "config", "doctor", "__run_turn"}
    try:
        command_index = next(index for index, value in enumerate(argv) if value in commands)
    except StopIteration:
        return argv
    if argv[command_index] == "__run_turn":
        return argv
    before = argv[:command_index]
    command = argv[command_index]
    after = argv[command_index + 1 :]
    moved: list[str] = []
    kept: list[str] = []
    index = 0
    while index < len(after):
        value = after[index]
        if value == "--json":
            moved.append(value)
            index += 1
        elif value == "--state-dir" and index + 1 < len(after):
            moved.extend([value, after[index + 1]])
            index += 2
        else:
            kept.append(value)
            index += 1
    return before + moved + [command] + kept


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(normalize_global_args(list(sys.argv[1:] if argv is None else argv)))
        return int(args.func(args))
    except CdxError as exc:
        eprint(f"error: {exc.message}")
        return exc.code
    except KeyboardInterrupt:
        eprint("error: interrupted; rerun status or result to inspect task state")
        return 5
    except Exception as exc:  # noqa: BLE001 - top-level contract forbids tracebacks.
        eprint(f"error: internal failure: {exc}")
        return 5


if __name__ == "__main__":
    sys.exit(main())
