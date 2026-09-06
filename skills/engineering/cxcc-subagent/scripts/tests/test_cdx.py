import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CDX = REPO_ROOT / "scripts" / "cdx.py"

spec = importlib.util.spec_from_file_location("cdx", CDX)
cdx = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(cdx)


def base_env(extra=None):
    """Subprocess env with any ambient harness session id stripped.

    cdx derives a task owner from the harness's session id, and the test runner is
    itself running inside a harness. Inheriting that would hand every test the same
    owner and quietly hide the very fallbacks these tests pin down."""
    env = os.environ.copy()
    env.pop("CDX_OWNER", None)
    for name, _ in cdx.HARNESS_SESSION_ENV:
        env.pop(name, None)
    if extra:
        env.update(extra)
    return env


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class ParserNormalizationTests(unittest.TestCase):
    """Pin that every documented call shape parses, on any supported python.

    argparse on some interpreters (seen on 3.12.4, fixed by 3.12.11) rejects an
    option between two positionals when the second is optional, which broke
    `send <task> --now "text"` before it ever reached a backend.
    normalize_global_args hoists options in front of positionals; these tests
    run the hoist plus a real parse in-process, so they stay deterministic and
    catch the regression on whichever python runs the suite."""

    def parse(self, argv):
        parser = cdx.build_parser()
        return parser.parse_args(cdx.normalize_global_args(argv, parser))

    def test_hoist_reorders_options_before_positionals(self):
        parser = cdx.build_parser()
        self.assertEqual(
            cdx.normalize_global_args(["send", "t", "--now", "prompt", "--json"], parser),
            ["send", "--now", "--json", "t", "prompt"],
        )

    def test_documented_send_shapes(self):
        for argv in (
            ["send", "t", "answer text", "--json"],
            ["send", "t", "--now", "stop, wrong approach", "--json"],
            ["send", "--json", "--state-dir", "/tmp/s", "t", "--stall-after", "120", "answer text"],
            ["send", "t", "--state-dir=/tmp/s", "answer text"],
        ):
            with self.subTest(argv=argv):
                args = self.parse(argv)
                self.assertEqual(args.task, "t")
                self.assertEqual(args.prompt, "answer text" if "answer text" in argv else "stop, wrong approach")

    def test_send_file_prompt_keeps_prompt_unset(self):
        args = self.parse(["send", "t", "-f", "a.md", "-f", "b.md", "--json"])
        self.assertEqual(args.file, ["a.md", "b.md"])
        self.assertIsNone(args.prompt)

    def test_spawn_options_between_positionals(self):
        args = self.parse(["spawn", "--json", "-C", "/tmp/repo", "--name", "n", "--stall-after", "120", "build it"])
        self.assertEqual(args.prompt, "build it")
        self.assertEqual(args.stall_after, 120)

    def test_double_dash_guards_option_lookalike_prompts(self):
        args = self.parse(["send", "--json", "t", "--", "--now is not a flag here"])
        self.assertTrue(args.json)
        self.assertEqual(args.prompt, "--now is not a flag here")

    def test_config_subcommand_with_trailing_globals(self):
        args = self.parse(["config", "set", "model.codex", "gpt-test", "--json", "--state-dir", "/tmp/s"])
        self.assertEqual((args.key, args.value), ("model.codex", "gpt-test"))
        self.assertTrue(args.json)

    def test_option_abbreviations_are_rejected(self):
        # an abbreviated option would bypass the hoist and hit the argparse bug,
        # so the parser refuses prefixes outright
        with self.assertRaises(cdx.CdxError) as ctx:
            with open(os.devnull, "w") as devnull:
                stderr, sys.stderr = sys.stderr, devnull
                try:
                    self.parse(["send", "t", "--stall", "120", "answer text"])
                finally:
                    sys.stderr = stderr
        self.assertEqual(ctx.exception.code, 2)

    def test_peek_thinking_optional_value_stays_in_place(self):
        args = self.parse(["peek", "t", "--thinking", "--json"])
        self.assertEqual(args.thinking, 1000)
        args = self.parse(["peek", "t", "--thinking", "500"])
        self.assertEqual(args.thinking, 500)


class StateDerivationTests(TempCase):
    def test_state_derivation_all_states(self):
        done_events = [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed"},
        ]
        question_events = [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "QUESTION: Need input?"}},
            {"type": "turn.completed"},
        ]
        failed_events = [{"type": "turn.started"}, {"type": "turn.failed", "message": "bad"}]
        self.assertEqual(cdx.derive_state({"state": "working"}, [], True), "working")
        self.assertEqual(cdx.derive_state({"state": "working"}, done_events, False), "done")
        self.assertEqual(cdx.derive_state({"state": "working"}, question_events, False), "awaiting_reply")
        self.assertEqual(cdx.derive_state({"state": "working"}, failed_events, False), "failed")
        self.assertEqual(cdx.derive_state({"state": "killed"}, [], False), "killed")
        self.assertEqual(cdx.derive_state({"state": "stalled"}, [], False), "stalled")

    def test_question_parsing_multiline(self):
        message = "Summary line\nQUESTION: Which branch?\nmain\nrelease"
        self.assertEqual(cdx.extract_question(message), "Which branch?\nmain\nrelease")
        self.assertIsNone(cdx.extract_question("No question here\nAlmost QUESTION: nope"))

    def test_claude_result_events_drive_state(self):
        base_meta = {"backend": "claude", "state": "working", "turns_launched": 1, "turn_launched_at": time.time() - 60}
        success_events = [
            {"type": "system", "session_id": "claude-session-1"},
            {"type": "assistant", "session_id": "claude-session-1", "message": {"content": [{"type": "text", "text": "fallback"}]}},
            {"type": "result", "session_id": "claude-session-1", "is_error": False, "result": "claude done"},
        ]
        error_events = [{"type": "result", "session_id": "claude-session-2", "is_error": True, "result": "bad"}]
        mid_turn_events = [
            {"type": "stream_event", "session_id": "claude-session-3", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "thinking"}}}
        ]
        self.assertEqual(cdx.derive_state(dict(base_meta), success_events, False), "done")
        self.assertEqual(cdx.last_agent_message(success_events, "claude"), "claude done")
        self.assertEqual(cdx.derive_state(dict(base_meta), error_events, False), "failed")
        self.assertEqual(cdx.derive_state(dict(base_meta), mid_turn_events, True), "working")

    def test_claude_session_id_capture_and_thinking_tail(self):
        events = [
            {"type": "stream_event", "session_id": "session-a", "event": {"delta": {"text": "alpha "}}},
            {"type": "stream_event", "session_id": "session-a", "event": {"delta": {"text": "beta "}}},
            {"type": "result", "session_id": "session-a", "is_error": False, "result": "ok"},
        ]
        self.assertEqual(cdx.newest_thread_id(events, "claude"), "session-a")
        self.assertEqual(cdx.claude_thinking_tail(events, 10), "lpha beta ")

    def test_effort_validation_uses_cdx_vocabulary(self):
        for effort in ("medium", "high", "max"):
            cdx.validate_effort(effort)
        with self.assertRaises(cdx.CdxError) as invalid:
            cdx.validate_effort("xhigh")
        self.assertEqual(invalid.exception.code, 2)
        self.assertIn("medium, high, max", invalid.exception.message)

    def test_effort_translation_in_spawn_and_resume_commands(self):
        repo = self.base / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        prompt_file = self.base / "prompt.md"
        prompt_file.write_text("hi", encoding="utf-8")
        expected = {
            "codex": {"medium": "medium", "high": "high", "max": "xhigh"},
            "claude": {"medium": "medium", "high": "high", "max": "xhigh"},
        }
        for backend, mappings in expected.items():
            for cdx_effort, backend_effort in mappings.items():
                meta = {
                    "backend": backend,
                    "repo": str(repo),
                    "thread_id": "thread-1",
                    "model": "model-a",
                    "effort": cdx_effort,
                }
                binary = f"{backend}-bin"
                spawn = cdx.backend_cmd(meta, prompt_file, "spawn", binary)
                resume = cdx.backend_cmd(meta, prompt_file, "resume", binary)
                if backend == "codex":
                    self.assertIn(f'model_reasoning_effort="{backend_effort}"', spawn)
                    self.assertIn(f'model_reasoning_effort="{backend_effort}"', resume)
                else:
                    self.assertEqual(spawn[spawn.index("--effort") + 1], backend_effort)
                    self.assertEqual(resume[resume.index("--effort") + 1], backend_effort)

    def test_codex_model_aliases_resolve_with_uniform_effort(self):
        repo = self.base / "repo-aliases"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        prompt = self.base / "aliases.md"
        prompt.write_text("hi", encoding="utf-8")
        cases = {
            (None, "medium"): ("gpt-5.6-sol", "medium"),
            ("sol", "high"): ("gpt-5.6-sol", "high"),
            ("sol", "max"): ("gpt-5.6-sol", "xhigh"),
            ("terra", "medium"): ("gpt-5.6-terra", "medium"),
            ("Terra", "high"): ("gpt-5.6-terra", "high"),
        }
        for (alias, cdx_effort), (model, provider_effort) in cases.items():
            self.assertEqual(cdx.resolve_execution("codex", cdx_effort, alias), (model, provider_effort))
            meta = {
                "backend": "codex",
                "repo": str(repo),
                "model": model,
                "effort": cdx_effort,
                "provider_effort": provider_effort,
            }
            command = cdx.backend_cmd(meta, prompt, "spawn", "codex-bin")
            self.assertEqual(command[command.index("-m") + 1], model)
            self.assertIn(f'model_reasoning_effort="{provider_effort}"', command)

    def test_explicit_codex_model_keeps_generic_effort_mapping(self):
        self.assertEqual(cdx.resolve_execution("codex", "medium", "gpt-test"), ("gpt-test", "medium"))
        self.assertEqual(cdx.resolve_execution("codex", "max", "gpt-test"), ("gpt-test", "xhigh"))

    def test_fable_has_model_specific_effort_mapping(self):
        expected = {"medium": "low", "high": "medium", "max": "high"}
        for model in ("fable", "Fable", "claude-fable-5-1", "claude-fable-5", "claude-fable-5-20260701"):
            for cdx_effort, provider_effort in expected.items():
                self.assertEqual(cdx.resolve_execution("claude", cdx_effort, model), ("claude-fable-5-1" if model.lower() == "fable" else model, provider_effort))
        self.assertEqual(cdx.resolve_execution("claude", "high", "sonnet"), ("sonnet", "high"))
        self.assertEqual(cdx.resolve_execution("claude", "max", "opus"), ("opus", "xhigh"))

    def test_premium_models_apply_effort_to_spawn_and_resume(self):
        prompt = self.base / "premium.md"
        prompt.write_text("hi")
        for backend, models in (("codex", ("astra", "Astra", "gpt-6-astra", "gpt-6-astra-20260901")),
                                ("claude", ("fable", "claude-fable-5-1"))):
            for requested in models:
                for effort, expected in (("medium", "low"), ("high", "medium"), ("max", "high")):
                    model, actual = cdx.resolve_execution(backend, effort, requested)
                    self.assertEqual(actual, expected)
                    if backend == "codex":
                        self.assertEqual(model, "gpt-6-astra" if requested.lower() == "astra" else requested)
                    meta = {"backend": backend, "repo": str(self.base), "model": model,
                            "effort": effort, "provider_effort": actual, "thread_id": "test-thread"}
                    for mode in ("spawn", "resume"):
                        command = cdx.backend_cmd(meta, prompt, mode, "provider-bin")
                        if backend == "codex":
                            self.assertIn(f'model_reasoning_effort="{expected}"', command)
                        else:
                            self.assertEqual(command[command.index("--effort") + 1], expected)

    def test_stored_provider_effort_is_authoritative(self):
        meta = {"backend": "codex", "model": "gpt-5.6-sol", "effort": "max", "provider_effort": "high"}
        self.assertEqual(cdx.resolved_provider_effort(meta), "high")

    def test_legacy_task_without_provider_effort_keeps_old_translation(self):
        meta = {"backend": "codex", "model": None, "effort": "medium"}
        self.assertEqual(cdx.resolved_provider_effort(meta), "medium")


class GrokBackendTests(TempCase):
    def grok_meta(self, **overrides):
        meta = {"backend": "grok", "repo": str(self.base), "model": None, "effort": None}
        meta.update(overrides)
        return meta

    def test_grok_spawn_and_resume_cmd_building(self):
        prompt = self.base / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        spawn = cdx.backend_cmd(self.grok_meta(model="grok-build", effort="high"), prompt, "spawn", "grok-bin")
        self.assertEqual(spawn[0], "grok-bin")
        self.assertEqual(spawn[spawn.index("--prompt-file") + 1], str(prompt))
        self.assertEqual(spawn[spawn.index("--output-format") + 1], "streaming-json")
        self.assertEqual(spawn[spawn.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(spawn[spawn.index("-m") + 1], "grok-build")
        # cdx "high" maps straight through to grok "high"
        self.assertEqual(spawn[spawn.index("--reasoning-effort") + 1], "high")
        self.assertNotIn("--resume", spawn)

        resume = cdx.backend_cmd(self.grok_meta(thread_id="sess-1", effort="max"), prompt, "resume", "grok-bin")
        self.assertEqual(resume[resume.index("--resume") + 1], "sess-1")
        self.assertEqual(resume[resume.index("--prompt-file") + 1], str(prompt))
        # cdx "max" maps to grok "xhigh", the same ceiling as codex and claude
        self.assertEqual(resume[resume.index("--reasoning-effort") + 1], "xhigh")

    def test_grok_resume_without_thread_id_errors(self):
        prompt = self.base / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        with self.assertRaises(cdx.CdxError) as ctx:
            cdx.backend_cmd(self.grok_meta(), prompt, "resume", "grok-bin")
        self.assertEqual(ctx.exception.code, 4)

    def test_grok_model_is_pinned_unless_overridden(self):
        model, effort = cdx.resolve_execution("grok", "high", None)
        self.assertEqual(model, cdx.GROK_DEFAULT_MODEL)
        self.assertEqual(effort, "high")
        # an explicit model still wins over the pin
        model, _ = cdx.resolve_execution("grok", "high", "grok-4.5")
        self.assertEqual(model, "grok-4.5")

    def test_grok_effort_mapping(self):
        self.assertEqual(cdx.BACKENDS["grok"].efforts, {"medium": "medium", "high": "high", "max": "xhigh"})
        self.assertEqual(cdx.backend_effort("grok", "medium"), "medium")
        self.assertIsNone(cdx.backend_effort("grok", None))

    def test_grok_event_parsing(self):
        events = [
            {"type": "thought", "data": "let me "},
            {"type": "thought", "data": "think"},
            {"type": "text", "data": "first "},
            {"type": "text", "data": "answer"},
            {"type": "end", "stopReason": "EndTurn", "sessionId": "sess-a"},
            {"type": "text", "data": "PING "},
            {"type": "text", "data": "reply"},
            {"type": "end", "stopReason": "EndTurn", "sessionId": "sess-b"},
        ]
        # thread_id is the sessionId of the newest end event
        self.assertEqual(cdx.newest_thread_id(events, "grok"), "sess-b")
        # turn_count is the number of end events
        self.assertEqual(cdx.turn_count(events, "grok"), 2)
        # last_agent_message assembles text deltas of the LAST completed turn only
        self.assertEqual(cdx.last_agent_message(events, "grok"), "PING reply")
        # thinking tail concatenates thought deltas from the end
        self.assertEqual(cdx.BACKENDS["grok"].thinking_tail(self.base, events, 100), "let me think")
        # mid-run (no end yet): no thread_id, no message
        mid = [{"type": "thought", "data": "x"}, {"type": "text", "data": "y"}]
        self.assertIsNone(cdx.newest_thread_id(mid, "grok"))
        self.assertIsNone(cdx.last_agent_message(mid, "grok"))
        # error event marks the run failed
        self.assertTrue(cdx.BACKENDS["grok"].failed([{"type": "error", "message": "boom"}]))
        self.assertFalse(cdx.BACKENDS["grok"].failed(events))

    def test_grok_state_derivation(self):
        base_meta = {"backend": "grok", "state": "working", "turns_launched": 1, "turn_launched_at": time.time() - 60}
        done_events = [
            {"type": "text", "data": "all "},
            {"type": "text", "data": "good"},
            {"type": "end", "stopReason": "EndTurn", "sessionId": "s1"},
        ]
        question_events = [
            {"type": "text", "data": "QUESTION: "},
            {"type": "text", "data": "which color?"},
            {"type": "end", "stopReason": "EndTurn", "sessionId": "s2"},
        ]
        # a completed turn that also carries an error event is failed
        failed_events = [
            {"type": "text", "data": "partial"},
            {"type": "end", "stopReason": "EndTurn", "sessionId": "s3"},
            {"type": "error", "message": "model unavailable"},
        ]
        error_only = [{"type": "error", "message": "boom"}]
        self.assertEqual(cdx.derive_state(dict(base_meta), done_events, False), "done")
        self.assertEqual(cdx.derive_state(dict(base_meta), question_events, False), "awaiting_reply")
        self.assertEqual(cdx.extract_question(cdx.last_agent_message(question_events, "grok")), "which color?")
        self.assertEqual(cdx.derive_state(dict(base_meta), failed_events, False), "failed")
        self.assertEqual(cdx.derive_state(dict(base_meta), error_only, False), "failed")

    def test_registry_derived_choices_and_config_keys(self):
        # config keys derive from the backend registry, adding model.grok automatically
        self.assertEqual(cdx.CONFIG_KEYS, {"model.codex", "model.claude", "model.grok"})
        cdx.require_config_key("model.grok")  # does not raise
        self.assertEqual(cdx.set_config_key({}, "model.grok", "grok-build"), {"model": {"grok": "grok-build"}})
        # argparse choices derive from the registry too
        parser = cdx.build_parser()
        parsed = parser.parse_args(["spawn", "-C", str(self.base), "--backend", "grok", "hello"])
        self.assertEqual(parsed.backend, "grok")
        with self.assertRaises(cdx.CdxError):
            parser.parse_args(["spawn", "-C", str(self.base), "--backend", "nonesuch", "hello"])


class TurnAccountingRaceTests(TempCase):
    def write_jsonl(self, path, events):
        with path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, separators=(",", ":")) + "\n")

    def make_race_task(self):
        state = self.base / "state"
        tdir = state / "tasks" / "race-task"
        tdir.mkdir(parents=True)
        (tdir / "turns").mkdir()
        first_turn = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "first done"}},
            {"type": "turn.completed"},
        ]
        self.write_jsonl(tdir / "events.jsonl", first_turn)
        (tdir / "stderr.log").write_text("stderr bytes\n", encoding="utf-8")
        cdx.save_meta(
            tdir,
            {
                "task": "race-task",
                "repo": str(self.base),
                "thread_id": "thread-1",
                "pid": 999999999,
                "spawned_at": time.time() - 60,
                "model": None,
                "effort": "high",
                "state": "working",
                "turns": 2,
                "turns_launched": 2,
                "turn_launched_at": time.time(),
                "last_exit_code": None,
            },
        )
        return state, tdir

    def run_cdx(self, args):
        return subprocess.run(
            [sys.executable, str(CDX), *args],
            cwd=self.base,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_send_race_stays_working_during_grace_and_result_waits_for_new_turn(self):
        state, tdir = self.make_race_task()
        events_path = tdir / "events.jsonl"

        status = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "race-task"])
        self.assertEqual(status.returncode, 0, status.stderr)
        data = json.loads(status.stdout)
        self.assertEqual(data["state"], "working")
        self.assertEqual(data["turns_launched"], 2)
        self.assertEqual(data["output_bytes"], cdx.combined_size(tdir))
        self.assertNotIn("events_last_60s", data)

        waiter = subprocess.Popen(
            [sys.executable, str(CDX), "result", "--json", "--state-dir", str(state), "race-task", "--wait", "--timeout", "5"],
            cwd=self.base,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            time.sleep(1.2)
            self.assertIsNone(waiter.poll(), "result --wait returned before the second turn completed")
            self.write_jsonl(
                events_path,
                [
                    {"type": "turn.started"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "second done"}},
                    {"type": "turn.completed"},
                ],
            )
            stdout, stderr = waiter.communicate(timeout=5)
        finally:
            if waiter.poll() is None:
                waiter.kill()
                waiter.communicate()

        self.assertEqual(waiter.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["message"], "second done")


def make_fake_codex(path: Path) -> Path:
    fake = path / "fake-codex.py"
    fake.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time

            def emit(obj):
                print(json.dumps(obj), flush=True)

            args = sys.argv[1:]
            if "--version" in args:
                print("fake-codex 1.0")
                sys.exit(0)
            if len(args) >= 2 and args[0] == "exec" and "--help" in args:
                print("Usage: codex exec [OPTIONS] [PROMPT]\\n      --json")
                sys.exit(0)

            if args[:2] == ["exec", "resume"] or (args[:1] == ["exec"] and "resume" in args):
                _prompt = sys.stdin.read()
                emit({"type": "turn.started"})
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "resumed done"}})
                emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
                sys.exit(0)

            if args[:1] == ["exec"]:
                prompt = sys.stdin.read()
                emit({"type": "thread.started", "thread_id": "fake-thread-1"})
                emit({"type": "turn.started"})
                if "STALL_MODE" in prompt:
                    sys.stderr.write("starting\\n")
                    sys.stderr.flush()
                    time.sleep(120)
                elif "QUESTION_MODE" in prompt:
                    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "QUESTION: First line\\nSecond line"}})
                    emit({"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 2}})
                elif "FAIL_MODE" in prompt:
                    emit({"type": "turn.failed", "message": "forced failure"})
                    sys.exit(1)
                else:
                    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "fake done"}})
                    emit({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 3}})
                sys.exit(0)
            print("unexpected fake codex args: " + repr(args), file=sys.stderr)
            sys.exit(2)
            """
        ),
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


class WatchdogTests(TempCase):
    def run_cdx(self, args, env=None, cwd=None):
        full_env = base_env(env)
        return subprocess.run(
            [sys.executable, str(CDX), *args],
            cwd=cwd or self.base,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
        )

    def poll_until(self, state, task, predicate, env, timeout=90):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), task], env=env)
            data = json.loads(last.stdout)
            if predicate(data):
                return last, data
            time.sleep(0.5)
        self.fail(f"condition never held; last={last.returncode if last else None} {last.stdout if last else ''} {last.stderr if last else ''}")

    def test_quiet_worker_is_reported_not_killed(self):
        # the watchdog cannot tell a hang from a worker inside a long test run, so
        # the soft threshold only raises a flag: the process keeps going untouched
        fake = make_fake_codex(self.base)
        state = self.base / "state"
        repo = self.base / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        env = {"CDX_CODEX_BIN": str(fake)}
        spawn = self.run_cdx(
            ["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "quiet-test", "--stall-after", "2", "--hard-kill-after", "0", "STALL_MODE"],
            env=env,
        )
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        status, data = self.poll_until(state, "quiet-test", lambda d: d["stall_suspect"], env)
        self.assertEqual(data["state"], "working")
        self.assertEqual(status.returncode, 0)
        self.assertTrue(data["pid_alive"], "the worker was killed even though only the soft threshold was set")
        self.assertGreaterEqual(data["quiet_for_s"], 2)
        # and it stays alive: the flag is a report, not a delayed kill
        time.sleep(5)
        still = json.loads(self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "quiet-test"], env=env).stdout)
        self.assertEqual((still["state"], still["pid_alive"]), ("working", True))
        self.run_cdx(["kill", "--json", "--state-dir", str(state), "quiet-test"], env=env)

    def test_hard_limit_still_kills_and_stays_resumable(self):
        fake = make_fake_codex(self.base)
        state = self.base / "state"
        repo = self.base / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        env = {"CDX_CODEX_BIN": str(fake)}
        spawn = self.run_cdx(
            ["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "stall-test", "--stall-after", "1", "--hard-kill-after", "4", "STALL_MODE"],
            env=env,
        )
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        # the soft flag comes first, the kill only at the separate hard limit
        _, flagged = self.poll_until(state, "stall-test", lambda d: d["stall_suspect"], env)
        self.assertEqual(flagged["state"], "working")
        last, data = self.poll_until(state, "stall-test", lambda d: d["state"] == "stalled", env)
        self.assertEqual(last.returncode, 0)
        self.assertIn("hard limit", data["stall_reason"])
        # a killed task is no longer a live suspect
        self.assertFalse(data["stall_suspect"])
        # stalled tasks resume via plain send — no --now gate (SKILL.md: `send "continue"`)
        send = self.run_cdx(["send", "--json", "--state-dir", str(state), "stall-test", "continue"], env=env)
        self.assertEqual(send.returncode, 0, send.stderr)
        result = self.run_cdx(["result", "--json", "--state-dir", str(state), "stall-test", "--wait", "--timeout", "60"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resumed done", json.loads(result.stdout)["message"])


class CliSubprocessTests(TempCase):
    def run_cdx(self, args, env=None, cwd=None):
        full_env = base_env(env)
        return subprocess.run(
            [sys.executable, str(CDX), *args],
            cwd=cwd or (self.base / "other"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
        )

    def assert_json_stdout(self, result):
        self.assertNotEqual(result.stdout, "", result.stderr)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"stdout was not pure JSON: {result.stdout!r}; stderr={result.stderr!r}; {exc}")

    def test_every_verb_json_and_exit_codes_from_different_cwd(self):
        fake = make_fake_codex(self.base)
        state = self.base / "state"
        repo = self.base / "repo"
        other = self.base / "other"
        repo.mkdir()
        other.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        env = {"CDX_CODEX_BIN": str(fake)}

        spawn = self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "cli-task", "hello"], env=env)
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        spawn_data = self.assert_json_stdout(spawn)
        self.assertEqual(spawn_data["model"], "gpt-5.6-sol")
        self.assertEqual(spawn_data["effort"], "medium")
        self.assertEqual(spawn_data["provider_effort"], "medium")

        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            status = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "cli-task"], env=env)
            data = self.assert_json_stdout(status)
            if data["state"] == "done":
                break
            time.sleep(0.2)
        self.assertIsNotNone(status)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(data["model"], "gpt-5.6-sol")
        self.assertEqual(data["provider_effort"], "medium")

        listing = self.run_cdx(["list", "--full", "--json", "--state-dir", str(state), "--all"], env=env)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        listing_data = self.assert_json_stdout(listing)
        self.assertIsInstance(listing_data, dict)
        tasks = listing_data["tasks"]
        self.assertEqual(tasks[0]["model"], "gpt-5.6-sol")
        self.assertEqual(tasks[0]["provider_effort"], "medium")

        peek = self.run_cdx(["peek", "--json", "--state-dir", str(state), "cli-task"], env=env)
        self.assertEqual(peek.returncode, 0, peek.stderr)
        self.assert_json_stdout(peek)

        result = self.run_cdx(["result", "--json", "--state-dir", str(state), "cli-task"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result_data = self.assert_json_stdout(result)
        self.assertEqual(result_data["message"], "fake done")
        self.assertEqual(result_data["model"], "gpt-5.6-sol")
        self.assertEqual(result_data["provider_effort"], "medium")

        send = self.run_cdx(["send", "--json", "--state-dir", str(state), "cli-task", "continue"], env=env)
        self.assertEqual(send.returncode, 0, send.stderr)
        self.assert_json_stdout(send)
        deadline = time.time() + 10
        while time.time() < deadline:
            status = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "cli-task"], env=env)
            data = self.assert_json_stdout(status)
            if data["state"] == "done":
                break
            time.sleep(0.2)
        self.assertEqual(status.returncode, 0, status.stderr)

        kill = self.run_cdx(["kill", "--json", "--state-dir", str(state), "cli-task"], env=env)
        self.assertEqual(kill.returncode, 0, kill.stderr)
        # spec: kill on an already-terminal task is a strict no-op — state is reported, not rewritten
        self.assertEqual(self.assert_json_stdout(kill)["state"], "done")
        status_after = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "cli-task"], env=env)
        self.assertEqual(self.assert_json_stdout(status_after)["state"], "done")

        doctor = self.run_cdx(["doctor", "--json", "--state-dir", str(state)], env=env)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertIn("checks", self.assert_json_stdout(doctor))

        clean_dry = self.run_cdx(["clean", "--json", "--state-dir", str(state), "--task", "cli-task", "--dry-run"], env=env)
        self.assertEqual(clean_dry.returncode, 0, clean_dry.stderr)
        self.assertEqual(self.assert_json_stdout(clean_dry)["would_remove"], ["cli-task"])

        clean = self.run_cdx(["clean", "--json", "--state-dir", str(state), "--task", "cli-task"], env=env)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(self.assert_json_stdout(clean)["removed"], ["cli-task"])

    def test_status_attention_exit_codes(self):
        fake = make_fake_codex(self.base)
        state = self.base / "state"
        repo = self.base / "repo"
        other = self.base / "other"
        repo.mkdir()
        other.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        env = {"CDX_CODEX_BIN": str(fake)}
        spawn = self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "question-task", "QUESTION_MODE"], env=env)
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        deadline = time.time() + 10
        while time.time() < deadline:
            status = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "question-task"], env=env)
            data = self.assert_json_stdout(status)
            if data["state"] == "awaiting_reply":
                self.assertEqual(status.returncode, 0)
                self.assertEqual(data["question"], "First line\nSecond line")
                return
            time.sleep(0.2)
        self.fail("question-task did not reach awaiting_reply")

    def test_invalid_effort_exits_2_and_names_choices(self):
        other = self.base / "other"
        other.mkdir()
        result = self.run_cdx(["spawn", "--json", "--state-dir", str(self.base / "state"), "-C", str(self.base), "--effort", "xhigh", "hello"])
        self.assertEqual(result.returncode, 2)
        # argparse quotes choices on 3.10/3.11 but not on 3.12+, so match each
        # choice individually instead of the joined list.
        self.assertIn("invalid choice", json.loads(result.stdout)["error"])
        for choice in ("medium", "high", "max"):
            self.assertIn(choice, json.loads(result.stdout)["error"])

    def test_spawn_help_explains_model_tiers_and_effort_dial(self):
        other = self.base / "other"
        other.mkdir()
        result = self.run_cdx(["spawn", "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sol|terra", result.stdout)
        self.assertIn("standard mapping", result.stdout)
        self.assertIn("Astra/Fable override", result.stdout)
        self.assertIn("medium=low", result.stdout)

    def test_config_get_set_unset_round_trip(self):
        other = self.base / "other"
        other.mkdir()
        state = self.base / "state-config"

        empty = self.run_cdx(["config", "get", "--json", "--state-dir", str(state)])
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(json.loads(empty.stdout), {})

        set_codex = self.run_cdx(["config", "set", "--json", "--state-dir", str(state), "model.codex", "gpt-test"])
        self.assertEqual(set_codex.returncode, 0, set_codex.stderr)
        self.assertEqual(json.loads(set_codex.stdout), {"model": {"codex": "gpt-test"}})

        set_claude = self.run_cdx(["config", "set", "--json", "--state-dir", str(state), "model.claude", "haiku"])
        self.assertEqual(set_claude.returncode, 0, set_claude.stderr)
        self.assertEqual(json.loads(set_claude.stdout), {"model": {"codex": "gpt-test", "claude": "haiku"}})

        get_config = self.run_cdx(["config", "get", "--json", "--state-dir", str(state)])
        self.assertEqual(get_config.returncode, 0, get_config.stderr)
        self.assertEqual(json.loads(get_config.stdout), {"model": {"codex": "gpt-test", "claude": "haiku"}})

        unset_codex = self.run_cdx(["config", "unset", "--json", "--state-dir", str(state), "model.codex"])
        self.assertEqual(unset_codex.returncode, 0, unset_codex.stderr)
        self.assertEqual(json.loads(unset_codex.stdout), {"model": {"claude": "haiku"}})

        unset_claude = self.run_cdx(["config", "unset", "--json", "--state-dir", str(state), "model.claude"])
        self.assertEqual(unset_claude.returncode, 0, unset_claude.stderr)
        self.assertEqual(json.loads(unset_claude.stdout), {})

    def test_model_resolution_precedence_flag_config_unset(self):
        fake = make_fake_codex(self.base)
        state = self.base / "state-model"
        repo = self.base / "repo-model"
        other = self.base / "other"
        repo.mkdir()
        other.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        env = {"CDX_CODEX_BIN": str(fake)}

        set_model = self.run_cdx(["config", "set", "--json", "--state-dir", str(state), "model.codex", "config-model"], env=env)
        self.assertEqual(set_model.returncode, 0, set_model.stderr)
        from_config = self.run_cdx(
            ["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "model-config", "hello"],
            env=env,
        )
        self.assertEqual(from_config.returncode, 0, from_config.stderr)
        self.assertEqual(json.loads(from_config.stdout)["model"], "config-model")
        self.assertEqual(cdx.load_meta(state / "tasks" / "model-config")["model"], "config-model")

        from_flag = self.run_cdx(
            ["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "model-flag", "--model", "flag-model", "hello"],
            env=env,
        )
        self.assertEqual(from_flag.returncode, 0, from_flag.stderr)
        self.assertEqual(json.loads(from_flag.stdout)["model"], "flag-model")
        self.assertEqual(cdx.load_meta(state / "tasks" / "model-flag")["model"], "flag-model")

        unset = self.run_cdx(["config", "unset", "--json", "--state-dir", str(state), "model.codex"], env=env)
        self.assertEqual(unset.returncode, 0, unset.stderr)
        unset_model = self.run_cdx(
            ["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "model-unset", "hello"],
            env=env,
        )
        self.assertEqual(unset_model.returncode, 0, unset_model.stderr)
        unset_data = json.loads(unset_model.stdout)
        self.assertEqual(unset_data["model"], "gpt-5.6-sol")
        self.assertEqual(unset_data["provider_effort"], "medium")
        unset_meta = cdx.load_meta(state / "tasks" / "model-unset")
        self.assertEqual(unset_meta["model"], "gpt-5.6-sol")
        self.assertEqual(unset_meta["provider_effort"], "medium")


@unittest.skipUnless(os.environ.get("CDX_LIVE_SMOKE") == "1", "set CDX_LIVE_SMOKE=1 to run paid provider smoke tests")
class RealBackendSmokeTests(TempCase):
    def run_cdx(self, args, env=None, cwd=None, timeout=120):
        full_env = base_env(env)
        return subprocess.run(
            [sys.executable, str(CDX), *args],
            cwd=cwd or self.base,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            timeout=timeout,
        )

    def poll_state(self, state, task, want, timeout=180):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), task], timeout=30)
            data = json.loads(last.stdout)
            if data["state"] == want:
                return last, data
            if data["state"] in {"failed", "stalled", "killed"} and want not in {"failed", "stalled", "killed"}:
                self.fail(f"{task} reached {data['state']} unexpectedly: {data}; stderr={last.stderr}")
            time.sleep(1)
        self.fail(f"{task} did not reach {want}; last={last.stdout if last else None} stderr={last.stderr if last else None}")

    def test_real_backend_trivial_file_task(self):
        codex_bin = shutil.which("codex")
        self.assertIsNotNone(codex_bin, "codex missing; install Codex CLI or set PATH")
        state = self.base / "state-real-file"
        repo = self.base / "real-file-repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        prompt = "Create hello.txt containing exactly 'hi', verify it, then summarize."
        spawn = self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "real-file", "--stall-after", "120", prompt], timeout=30)
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        json.loads(spawn.stdout)
        status, _ = self.poll_state(state, "real-file", "done", timeout=240)
        self.assertEqual(status.returncode, 0, status.stderr)
        result = self.run_cdx(["result", "--json", "--state-dir", str(state), "real-file"], timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)["message"]
        self.assertTrue(message.strip())
        self.assertEqual((repo / "hello.txt").read_text(encoding="utf-8").strip(), "hi")

    def test_real_backend_question_round_trip(self):
        codex_bin = shutil.which("codex")
        self.assertIsNotNone(codex_bin, "codex missing; install Codex CLI or set PATH")
        state = self.base / "state-real-question"
        repo = self.base / "real-question-repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        prompt = "For this orchestration test, do not make files yet. End your turn with exactly: QUESTION: What content should answer.txt contain?"
        spawn = self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "real-question", "--stall-after", "120", prompt], timeout=30)
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        self.poll_state(state, "real-question", "awaiting_reply", timeout=240)
        status = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "real-question"], timeout=30)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("answer.txt", json.loads(status.stdout)["question"])
        send = self.run_cdx(
            ["send", "--json", "--state-dir", str(state), "real-question", "--stall-after", "120", "Use hi. Create answer.txt containing exactly hi, verify it, then summarize."],
            timeout=30,
        )
        self.assertEqual(send.returncode, 0, send.stderr)
        done, _ = self.poll_state(state, "real-question", "done", timeout=240)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_real_claude_backend_trivial_file_and_resume(self):
        claude_bin = shutil.which("claude")
        self.assertIsNotNone(claude_bin, "claude missing; install Claude Code or set PATH")
        state = self.base / "state-real-claude"
        repo = self.base / "real-claude-repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        prompt = "Create hello.txt containing exactly 'hi', verify it, then summarize."
        spawn = self.run_cdx(
            [
                "spawn",
                "--json",
                "--state-dir",
                str(state),
                "-C",
                str(repo),
                "--name",
                "real-claude",
                "--backend",
                "claude",
                "--model",
                "haiku",
                "--effort",
                "medium",
                "--stall-after",
                "180",
                prompt,
            ],
            timeout=30,
        )
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        spawn_data = json.loads(spawn.stdout)
        self.assertEqual(spawn_data["backend"], "claude")
        status, status_data = self.poll_state(state, "real-claude", "done", timeout=300)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status_data["backend"], "claude")
        self.assertTrue(status_data["thread_id"])
        result = self.run_cdx(["result", "--json", "--state-dir", str(state), "real-claude"], timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)["message"]
        self.assertTrue(message.strip())
        self.assertEqual((repo / "hello.txt").read_text(encoding="utf-8").strip(), "hi")

        followup = "Reply with the exact phrase FOLLOWUP-OK and do not edit files."
        send = self.run_cdx(
            ["send", "--json", "--state-dir", str(state), "real-claude", "--stall-after", "180", followup],
            timeout=30,
        )
        self.assertEqual(send.returncode, 0, send.stderr)
        send_data = json.loads(send.stdout)
        self.assertEqual(send_data["backend"], "claude")
        done, _ = self.poll_state(state, "real-claude", "done", timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr)
        followup_result = self.run_cdx(["result", "--json", "--state-dir", str(state), "real-claude"], timeout=30)
        self.assertEqual(followup_result.returncode, 0, followup_result.stderr)
        self.assertIn("FOLLOWUP-OK", json.loads(followup_result.stdout)["message"])

    def test_real_grok_backend_trivial_file_and_resume(self):
        grok_bin = shutil.which("grok")
        self.assertIsNotNone(grok_bin, "grok missing; install Grok CLI or set PATH")
        state = self.base / "state-real-grok"
        repo = self.base / "real-grok-repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        prompt = "Create hello.txt containing exactly 'hi', verify it, then summarize."
        spawn = self.run_cdx(
            ["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "real-grok", "--backend", "grok", "--stall-after", "180", prompt],
            timeout=30,
        )
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        spawn_data = json.loads(spawn.stdout)
        self.assertEqual(spawn_data["backend"], "grok")
        status, status_data = self.poll_state(state, "real-grok", "done", timeout=300)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status_data["backend"], "grok")
        self.assertTrue(status_data["thread_id"])
        self.assertEqual((repo / "hello.txt").read_text(encoding="utf-8").strip(), "hi")

        followup = "Reply with the exact phrase FOLLOWUP-OK and do not edit files."
        send = self.run_cdx(
            ["send", "--json", "--state-dir", str(state), "real-grok", "--stall-after", "180", followup],
            timeout=30,
        )
        self.assertEqual(send.returncode, 0, send.stderr)
        self.assertEqual(json.loads(send.stdout)["backend"], "grok")
        done, _ = self.poll_state(state, "real-grok", "done", timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr)
        followup_result = self.run_cdx(["result", "--json", "--state-dir", str(state), "real-grok"], timeout=30)
        self.assertEqual(followup_result.returncode, 0, followup_result.stderr)
        self.assertIn("FOLLOWUP-OK", json.loads(followup_result.stdout)["message"])


class OwnerScopingTests(TempCase):
    def run_cdx(self, args, env=None, cwd=None):
        full_env = base_env(env)
        return subprocess.run(
            [sys.executable, str(CDX), *args],
            cwd=cwd or self.base,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
        )

    def git_repo(self, name):
        repo = self.base / name
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        return repo

    def spawn_done(self, state, repo, name, owner):
        env = {"CDX_CODEX_BIN": self.fake_bin, "CDX_OWNER": owner}
        spawn = self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", name, "hello"], env=env)
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        deadline = time.time() + 30
        while time.time() < deadline:
            status = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), name], env={"CDX_CODEX_BIN": self.fake_bin})
            if json.loads(status.stdout)["state"] == "done":
                return
            time.sleep(0.2)
        self.fail(f"{name} did not reach done")

    def test_remove_task_dir_tolerates_finalize_race(self):
        # the detached supervisor may write meta.json once as clean removes the dir.
        # remove_task_dir must never leave the directory half-removed (an earlier naive
        # rmtree left it behind ~2/3 of the time and mislabeled it as a running task).
        import threading

        left_behind = 0
        for _ in range(200):
            tdir = self.base / "runner"
            (tdir / "turns").mkdir(parents=True)
            for name in ("meta.json", "events.jsonl", "stderr.log"):
                (tdir / name).write_text("x" * 200, encoding="utf-8")
            barrier = threading.Barrier(2)

            def finalize_once():
                barrier.wait()
                try:
                    cdx.finalize_meta(tdir, {"state": "done"})
                except OSError:
                    pass

            worker = threading.Thread(target=finalize_once)
            worker.start()
            barrier.wait()
            result = cdx.remove_task_dir(tdir)
            worker.join(timeout=1)
            if tdir.exists():
                left_behind += 1
            self.assertIn(result, ("removed", "finalizing"))
            shutil.rmtree(tdir, ignore_errors=True)
        self.assertEqual(left_behind, 0, "remove_task_dir left task dirs half-removed under the finalize race")

    def test_clean_terminal_is_owner_scoped(self):
        self.fake_bin = str(make_fake_codex(self.base))
        state = self.base / "state"
        repo = self.git_repo("repo")
        self.spawn_done(state, repo, "alice-one", "alice")
        self.spawn_done(state, repo, "alice-two", "alice")
        self.spawn_done(state, repo, "bob-one", "bob")

        # owner is surfaced in status and list
        status = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "alice-one"], env={"CDX_CODEX_BIN": self.fake_bin})
        self.assertEqual(json.loads(status.stdout)["owner"], "alice")

        # alice's sweep leaves bob's uncollected task and reports the skip
        clean = self.run_cdx(["clean", "--json", "--state-dir", str(state), "--terminal"], env={"CDX_CODEX_BIN": self.fake_bin, "CDX_OWNER": "alice"})
        self.assertEqual(clean.returncode, 0, clean.stderr)
        data = json.loads(clean.stdout)
        self.assertEqual(sorted(data["removed"]), ["alice-one", "alice-two"])
        self.assertEqual(data["skipped_foreign"], 1)

        listing = self.run_cdx(["list", "--full", "--json", "--state-dir", str(state), "--all", "--any-owner"], env={"CDX_CODEX_BIN": self.fake_bin})
        remaining = json.loads(listing.stdout)["tasks"]
        self.assertEqual([t["task"] for t in remaining], ["bob-one"])
        self.assertEqual(remaining[0]["owner"], "bob")

        # --any-owner is the deliberate global sweep
        clean_any = self.run_cdx(["clean", "--json", "--state-dir", str(state), "--terminal", "--any-owner"], env={"CDX_CODEX_BIN": self.fake_bin, "CDX_OWNER": "alice"})
        self.assertEqual(json.loads(clean_any.stdout)["removed"], ["bob-one"])

    def test_list_is_owner_scoped(self):
        # two chats sharing one machine: each session's list shows only its own fleet,
        # foreign tasks surface as a count, --any-owner is the deliberate global view
        self.fake_bin = str(make_fake_codex(self.base))
        state = self.base / "state"
        repo = self.git_repo("repo")
        other_repo = self.git_repo("other-repo")
        self.spawn_done(state, repo, "alice-one", "alice")
        self.spawn_done(state, other_repo, "bob-one", "bob")

        as_alice = {"CDX_CODEX_BIN": self.fake_bin, "CDX_OWNER": "alice"}
        scoped = self.run_cdx(["list", "--full", "--json", "--state-dir", str(state), "--all"], env=as_alice)
        self.assertEqual(scoped.returncode, 0, scoped.stderr)
        data = json.loads(scoped.stdout)
        self.assertEqual([t["task"] for t in data["tasks"]], ["alice-one"])
        self.assertEqual(data["skipped_foreign"], 1)

        global_view = json.loads(self.run_cdx(["list", "--full", "--json", "--state-dir", str(state), "--all", "--any-owner"], env=as_alice).stdout)
        self.assertEqual(sorted(t["task"] for t in global_view["tasks"]), ["alice-one", "bob-one"])
        self.assertEqual(global_view["skipped_foreign"], 0)

        # -C <repo> pulls in that repo's tasks across owners, on top of your own
        repo_view = json.loads(self.run_cdx(["list", "--full", "--json", "--state-dir", str(state), "--all", "-C", str(other_repo)], env=as_alice).stdout)
        self.assertEqual(sorted(t["task"] for t in repo_view["tasks"]), ["alice-one", "bob-one"])
        self.assertEqual(repo_view["skipped_foreign"], 0)

    def test_clean_repo_filter_reaps_across_owners(self):
        # a task spawned with -C for a repo, under a different owner (e.g. a different
        # spawning cwd): a plain sweep treats it as foreign, but `clean -C <repo>` reaps it.
        self.fake_bin = str(make_fake_codex(self.base))
        state = self.base / "state"
        repo = self.git_repo("repo")
        self.spawn_done(state, repo, "cross-task", "elsewhere")

        here = {"CDX_CODEX_BIN": self.fake_bin, "CDX_OWNER": "here"}
        plain = self.run_cdx(["clean", "--json", "--state-dir", str(state), "--terminal"], env=here)
        plain_data = json.loads(plain.stdout)
        self.assertEqual(plain_data["removed"], [])
        self.assertEqual(plain_data["skipped_foreign"], 1)

        scoped = self.run_cdx(["clean", "--json", "--state-dir", str(state), "--terminal", "-C", str(repo)], env=here)
        self.assertEqual(json.loads(scoped.stdout)["removed"], ["cross-task"])

    def test_owner_defaults_to_cwd_when_env_unset(self):
        self.fake_bin = str(make_fake_codex(self.base))
        state = self.base / "state"
        repo = self.git_repo("repo")
        worktree = self.base / "worktreeA"
        worktree.mkdir()
        env = {"CDX_CODEX_BIN": self.fake_bin, "CDX_OWNER": ""}  # empty → falls back to cwd
        spawn = self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "cwd-task", "hi"], env=env, cwd=worktree)
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        status = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "cwd-task"], env=env)
        self.assertEqual(json.loads(status.stdout)["owner"], str(worktree.resolve()))

    def start_running_task(self, state, repo, name, env):
        spawn = self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", name, "--stall-after", "600", "STALL_MODE"], env=env)
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        pid = None
        deadline = time.time() + 30  # generous: only the failure path waits this long
        while time.time() < deadline:
            status = json.loads(self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), name], env=env).stdout)
            if status.get("pid") and status.get("pid_alive"):
                pid = status["pid"]
                break
            time.sleep(0.2)
        self.assertIsNotNone(pid, f"{name} never reported a live pid")
        return pid

    def test_clean_never_removes_a_running_task(self):
        self.fake_bin = str(make_fake_codex(self.base))
        state = self.base / "state"
        repo = self.git_repo("repo")
        env = {"CDX_CODEX_BIN": self.fake_bin, "CDX_OWNER": "solo"}
        pid = self.start_running_task(state, repo, "runner", env)
        runner_dir = state / "tasks" / "runner"

        # --all sees the running task but refuses to delete it, reporting it as skipped
        clean = self.run_cdx(["clean", "--json", "--state-dir", str(state), "--all"], env=env)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        data = json.loads(clean.stdout)
        self.assertEqual(data["removed"], [])
        self.assertEqual(data["skipped_running"], 1)
        self.assertTrue(runner_dir.exists(), "running task dir must survive clean --all")
        self.assertTrue(cdx.pid_alive(pid), "running backend must not be touched by clean")

        # cleaning it by name errors (kill first), rather than racing the supervisor
        by_name = self.run_cdx(["clean", "--json", "--state-dir", str(state), "--task", "runner"], env=env)
        self.assertEqual(by_name.returncode, 1, by_name.stdout)
        self.assertTrue(runner_dir.exists())

        # kill first: it moves the task to a terminal state, which is what clean needs.
        # (pid liveness can flap briefly while the OS reaps the killed zombie, so we key
        # on state, not on the pid, exactly as clean itself does.)
        kill = self.run_cdx(["kill", "--json", "--state-dir", str(state), "runner"], env=env)
        self.assertEqual(kill.returncode, 0, kill.stderr)
        deadline = time.time() + 30
        killed_state = None
        while time.time() < deadline:
            killed_state = json.loads(self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "runner"], env=env).stdout)["state"]
            if killed_state in cdx.TERMINAL_STATES:
                break
            time.sleep(0.2)
        self.assertIn(killed_state, cdx.TERMINAL_STATES)
        # clean is re-runnable: if the supervisor is mid-finalize it's skipped, so poll
        removed = []
        deadline = time.time() + 30
        while time.time() < deadline:
            removed = json.loads(self.run_cdx(["clean", "--json", "--state-dir", str(state), "--all"], env=env).stdout)["removed"]
            if "runner" in removed:
                break
            time.sleep(0.3)
        self.assertEqual(removed, ["runner"])
        # and it stays gone: the supervisor never resurrects a removed dir
        deadline = time.time() + 3
        while time.time() < deadline:
            self.assertFalse(runner_dir.exists(), "killed+cleaned task dir was resurrected")
            time.sleep(0.2)
        listing = self.run_cdx(["list", "--full", "--json", "--state-dir", str(state), "--all"], env=env)
        self.assertEqual(json.loads(listing.stdout)["tasks"], [])
        self.assertEqual(json.loads(listing.stdout)["count"], 0)


def row(task, state, **extra):
    base = {"task": task, "state": state, "backend": "codex", "repo": "/repo", "age_s": 10, "last_output_age_s": 1, "last_activity": None, "question": None}
    base.update(extra)
    return base


class WatchStateTests(unittest.TestCase):
    """The transition and heartbeat rules `watch` streams, driven by a fake clock."""

    def test_arming_snapshot_then_silence(self):
        tracker = cdx.WatchState(heartbeat=600)
        events = tracker.step([row("a", "working")], 0, 0.0)
        self.assertEqual([e["event"] for e in events], ["armed"])
        # names and states only: the caller spawned these, it knows the rest
        self.assertEqual(events[0]["tasks"], {"a": "working"})
        # no change and no heartbeat due: nothing at all
        self.assertEqual(tracker.step([row("a", "working")], 0, 15.0), [])

    def test_change_out_of_working_is_one_event(self):
        tracker = cdx.WatchState(heartbeat=600)
        tracker.step([row("a", "working")], 0, 0.0)
        events = tracker.step([row("a", "done")], 0, 15.0)
        self.assertEqual([e["event"] for e in events], ["change"])
        self.assertEqual((events[0]["previous_state"], events[0]["state"]), ("working", "done"))
        # the same state is not re-announced on every poll
        self.assertEqual(tracker.step([row("a", "done")], 0, 30.0), [])

    def test_escalated_question_rides_along_with_the_change(self):
        tracker = cdx.WatchState(heartbeat=600)
        tracker.step([row("a", "working")], 0, 0.0)
        events = tracker.step([row("a", "awaiting_reply", question="Which schema should I use?")], 0, 15.0)
        self.assertEqual(events[0]["question"], "Which schema should I use?")

    def test_fresh_spawn_is_seeded_silently_but_a_new_terminal_task_is_not(self):
        tracker = cdx.WatchState(heartbeat=600)
        tracker.step([], 0, 0.0)
        # our own spawn: the orchestrator just made this happen, it is not news
        self.assertEqual(tracker.step([row("a", "working")], 0, 15.0), [])
        # a task that appears already terminal (e.g. it finished between two polls) is
        events = tracker.step([row("a", "working"), row("b", "failed")], 0, 30.0)
        self.assertEqual([(e["task"], e["state"]) for e in events], [("b", "failed")])

    def test_cleaned_task_disappearing_is_not_an_event(self):
        tracker = cdx.WatchState(heartbeat=600)
        tracker.step([row("a", "working"), row("b", "done")], 0, 0.0)
        self.assertEqual(tracker.step([row("a", "working")], 0, 15.0), [])
        self.assertNotIn("b", tracker.seen)

    def test_heartbeat_ticks_only_while_something_works(self):
        tracker = cdx.WatchState(heartbeat=600)
        tracker.step([row("a", "working")], 0, 0.0)
        self.assertEqual(tracker.step([row("a", "working")], 0, 599.0), [])
        beat = tracker.step([row("a", "working")], 0, 600.0)
        self.assertEqual([e["event"] for e in beat], ["heartbeat"])
        # a sketch, not rows: one string per task, nothing the caller already holds
        self.assertEqual(beat[0]["working"], ["a working 10s"])
        self.assertNotIn("uncollected", beat[0])
        # uncollected results ride along, so a lost change event still resurfaces
        tracker.step([row("a", "working"), row("b", "done")], 0, 601.0)
        beat = tracker.step([row("a", "working"), row("b", "done")], 0, 1300.0)
        self.assertEqual(beat[0]["uncollected"], ["b done 10s"])

    def test_heartbeat_stops_when_nothing_is_working(self):
        # an idle session has no chat to keep awake: silence is correct there
        tracker = cdx.WatchState(heartbeat=600)
        tracker.step([row("a", "done")], 0, 0.0)
        self.assertEqual(tracker.step([row("a", "done")], 0, 5000.0), [])

    def test_a_change_resets_the_heartbeat_timer(self):
        # a change line wakes the session just as well as a heartbeat, so it counts
        tracker = cdx.WatchState(heartbeat=600)
        tracker.step([row("a", "working"), row("b", "working")], 0, 0.0)
        self.assertEqual([e["event"] for e in tracker.step([row("a", "working"), row("b", "done")], 0, 590.0)], ["change"])
        self.assertEqual(tracker.step([row("a", "working"), row("b", "done")], 0, 900.0), [])
        self.assertEqual([e["event"] for e in tracker.step([row("a", "working"), row("b", "done")], 0, 1190.0)], ["heartbeat"])

    def test_quiet_flag_and_recovery_are_their_own_events(self):
        # the task never leaves `working`, so a plain state diff would miss both
        tracker = cdx.WatchState(heartbeat=600)
        tracker.step([row("a", "working")], 0, 0.0)
        flagged = tracker.step([row("a", "working", stall_suspect=True, quiet_for_s=300)], 0, 15.0)
        self.assertEqual([e["event"] for e in flagged], ["stall_suspect"])
        self.assertEqual(flagged[0]["quiet_for_s"], 300)
        # a standing suspicion is not re-announced on every poll
        self.assertEqual(tracker.step([row("a", "working", stall_suspect=True)], 0, 30.0), [])
        self.assertEqual([e["event"] for e in tracker.step([row("a", "working")], 0, 45.0)], ["recovered"])

    def test_a_suspect_that_finishes_reports_the_state_change_only(self):
        tracker = cdx.WatchState(heartbeat=600)
        tracker.step([row("a", "working", stall_suspect=True)], 0, 0.0)
        events = tracker.step([row("a", "done")], 0, 15.0)
        self.assertEqual([(e["event"], e["state"]) for e in events], [("change", "done")])

    def test_the_heartbeat_marks_a_quiet_task_inline(self):
        # a standing suspicion has to stay visible without being its own entry
        tracker = cdx.WatchState(heartbeat=600)
        tracker.step([row("a", "working", stall_suspect=True, quiet_for_s=320)], 0, 0.0)
        beat = tracker.step([row("a", "working", stall_suspect=True, quiet_for_s=320)], 0, 600.0)
        self.assertEqual(beat[0]["working"], ["a working 10s quiet 5m"])

    def test_heartbeat_zero_disables_the_tick(self):
        tracker = cdx.WatchState(heartbeat=0)
        tracker.step([row("a", "working")], 0, 0.0)
        self.assertEqual(tracker.step([row("a", "working")], 0, 100000.0), [])


class WatchCliTests(TempCase):
    def run_cdx(self, args, env=None, cwd=None):
        full_env = base_env(env)
        return subprocess.run([sys.executable, str(CDX), *args], cwd=cwd or self.base, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full_env)

    def test_watch_streams_a_change_and_is_owner_scoped(self):
        fake = make_fake_codex(self.base)
        state = self.base / "state"
        repo = self.base / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        env = base_env({"CDX_CODEX_BIN": str(fake), "CDX_OWNER": "alice"})

        # a foreign session's task must never show up in alice's stream
        self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "bob-task", "hello"], env={"CDX_CODEX_BIN": str(fake), "CDX_OWNER": "bob"})
        spawn = self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "alice-task", "--stall-after", "120", "STALL_MODE"], env={"CDX_CODEX_BIN": str(fake), "CDX_OWNER": "alice"})
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        # let the task actually get going: the supervisor's startup write would
        # otherwise clobber a state we stamp in the same breath as the spawn
        deadline = time.time() + 30
        while time.time() < deadline:
            if json.loads(self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "alice-task"], env={"CDX_CODEX_BIN": str(fake)}).stdout)["output_bytes"]:
                break
            time.sleep(0.2)

        watcher = subprocess.Popen(
            [sys.executable, str(CDX), "watch", "--json", "--state-dir", str(state), "--interval", "1", "--heartbeat", "0"],
            cwd=self.base,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            armed = json.loads(watcher.stdout.readline())
            self.assertEqual(armed["event"], "armed")
            self.assertEqual(list(armed["tasks"]), ["alice-task"])
            self.assertEqual(armed["skipped_foreign"], 1)

            # the line arrives on its own, without the watcher exiting: that is the
            # whole point of the verb (a wake per event, not a wake per process death)
            self.assertIsNone(watcher.poll())
            self.run_cdx(["kill", "--json", "--state-dir", str(state), "alice-task"], env={"CDX_CODEX_BIN": str(fake)})
            change = json.loads(watcher.stdout.readline())
            self.assertEqual(change["event"], "change")
            self.assertEqual(change["task"], "alice-task")
            self.assertEqual(change["state"], "killed")
            self.assertIsNone(watcher.poll(), "watch exited after an event instead of staying armed")
        finally:
            watcher.terminate()
            watcher.communicate(timeout=10)

    def test_result_wait_timeout_is_still_working_not_an_error(self):
        fake = make_fake_codex(self.base)
        state = self.base / "state"
        repo = self.base / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        env = {"CDX_CODEX_BIN": str(fake)}
        spawn = self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "slow-task", "--stall-after", "120", "STALL_MODE"], env=env)
        self.assertEqual(spawn.returncode, 0, spawn.stderr)

        result = self.run_cdx(["result", "--json", "--state-dir", str(state), "slow-task", "--wait", "--timeout", "2"], env=env)
        # exit 10 (still working), a real JSON payload on stdout, and nothing that
        # reads as "the agent died and needs resuming"
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["state"], "working")
        self.assertEqual(data["reason"], "timeout")
        self.assertGreaterEqual(data["waited_s"], 2)
        self.assertNotIn("error", result.stderr)
        # and the task itself was not touched by the expiry
        status = self.run_cdx(["status", "--full", "--json", "--state-dir", str(state), "slow-task"], env=env)
        self.assertEqual(json.loads(status.stdout)["state"], "working")
        self.run_cdx(["kill", "--json", "--state-dir", str(state), "slow-task"], env=env)

    def test_result_without_wait_reports_working_as_json(self):
        fake = make_fake_codex(self.base)
        state = self.base / "state"
        repo = self.base / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        env = {"CDX_CODEX_BIN": str(fake)}
        self.run_cdx(["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "slow-task", "--stall-after", "120", "STALL_MODE"], env=env)
        result = self.run_cdx(["result", "--json", "--state-dir", str(state), "slow-task"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        # --json used to promise pure JSON on stdout and then print nothing here
        data = json.loads(result.stdout)
        self.assertEqual((data["state"], data["reason"]), ("working", "no_wait"))
        self.run_cdx(["kill", "--json", "--state-dir", str(state), "slow-task"], env=env)


class WaitCliTests(TempCase):
    def run_cdx(self, args, env=None, cwd=None):
        full_env = base_env(env)
        return subprocess.run([sys.executable, str(CDX), *args], cwd=cwd or self.base, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full_env)

    def setup_repo(self):
        self.fake = str(make_fake_codex(self.base))
        self.state = self.base / "state"
        repo = self.base / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        return repo

    def test_wait_returns_on_the_change_with_the_whole_fleet(self):
        repo = self.setup_repo()
        env = {"CDX_CODEX_BIN": self.fake, "CDX_OWNER": "alice"}
        self.run_cdx(["spawn", "--json", "--state-dir", str(self.state), "-C", str(repo), "--name", "alice-task", "--stall-after", "120", "STALL_MODE"], env=env)
        # a foreign session's task must not be able to end alice's wait
        self.run_cdx(["spawn", "--json", "--state-dir", str(self.state), "-C", str(repo), "--name", "bob-task", "hello"], env={"CDX_CODEX_BIN": self.fake, "CDX_OWNER": "bob"})
        deadline = time.time() + 30
        while time.time() < deadline:
            if json.loads(self.run_cdx(["status", "--full", "--json", "--state-dir", str(self.state), "alice-task"], env={"CDX_CODEX_BIN": self.fake}).stdout)["output_bytes"]:
                break
            time.sleep(0.2)

        killer = subprocess.Popen([sys.executable, "-c", f"import subprocess,sys,time; time.sleep(3); subprocess.run([sys.executable, {str(CDX)!r}, 'kill', '--json', '--state-dir', {str(self.state)!r}, 'alice-task'])"], env={**os.environ, "CDX_CODEX_BIN": self.fake})
        try:
            wait = self.run_cdx(["wait", "--json", "--state-dir", str(self.state), "--timeout", "60", "--interval", "1"], env=env)
        finally:
            killer.wait(timeout=30)
        self.assertEqual(wait.returncode, 0, wait.stderr)
        data = json.loads(wait.stdout)
        self.assertEqual(data["reason"], "change")
        self.assertEqual([(e["task"], e["state"]) for e in data["events"]], [("alice-task", "killed")])
        # the events are the news; the fleet is context the caller already holds,
        # so a change carries only what is still running (here: nothing)
        self.assertEqual(data["working"], [])
        self.assertNotIn("tasks", data)
        self.assertEqual(data["skipped_foreign"], 1)
        # and no field on the event repeats what the caller chose at spawn
        self.assertEqual(set(data["events"][0]) - {"event", "previous_state", "task", "state", "age_s", "last_activity"}, set())

    def test_wait_expiry_is_exit_zero_and_leaves_the_task_running(self):
        repo = self.setup_repo()
        env = {"CDX_CODEX_BIN": self.fake, "CDX_OWNER": "alice"}
        self.run_cdx(["spawn", "--json", "--state-dir", str(self.state), "-C", str(repo), "--name", "alice-task", "--stall-after", "120", "STALL_MODE"], env=env)
        wait = self.run_cdx(["wait", "--json", "--state-dir", str(self.state), "--timeout", "2", "--interval", "1"], env=env)
        # the whole point: an expiry is a check-in, not an error and not a kill
        self.assertEqual(wait.returncode, 0, wait.stderr)
        data = json.loads(wait.stdout)
        self.assertEqual(data["reason"], "timeout")
        # nothing moved, so there is nothing to report but the shape of the fleet
        self.assertEqual(len(data["working"]), 1)
        self.assertTrue(data["working"][0].startswith("alice-task working "), data["working"])
        self.assertNotIn("events", data)
        self.assertEqual(json.loads(self.run_cdx(["status", "--full", "--json", "--state-dir", str(self.state), "alice-task"], env=env).stdout)["state"], "working")
        self.run_cdx(["kill", "--json", "--state-dir", str(self.state), "alice-task"], env=env)

    def test_wait_with_nothing_running_returns_at_once(self):
        # otherwise a caller that loops on wait blocks for ten minutes after the
        # last task finished, or spins on an always-ready wait
        repo = self.setup_repo()
        env = {"CDX_CODEX_BIN": self.fake, "CDX_OWNER": "alice"}
        self.run_cdx(["spawn", "--json", "--state-dir", str(self.state), "-C", str(repo), "--name", "alice-task", "hello"], env=env)
        deadline = time.time() + 30
        while time.time() < deadline:
            if json.loads(self.run_cdx(["status", "--full", "--json", "--state-dir", str(self.state), "alice-task"], env=env).stdout)["state"] == "done":
                break
            time.sleep(0.2)
        start = time.time()
        wait = self.run_cdx(["wait", "--json", "--state-dir", str(self.state), "--timeout", "600"], env=env)
        self.assertLess(time.time() - start, 15)
        data = json.loads(wait.stdout)
        self.assertEqual(data["reason"], "idle")
        self.assertEqual([t["state"] for t in data["tasks"]], ["done"])


class OwnerFallbackTests(TempCase):
    def test_precedence_explicit_then_harness_then_cwd(self):
        cwd = str(Path.cwd().resolve())
        for env, expected in (
            ({"CDX_OWNER": "chat-slug", "CODEX_THREAD_ID": "thread-9"}, "chat-slug"),
            ({"CODEX_THREAD_ID": "thread-9"}, "codex:thread-9"),
            ({"CLAUDE_CODE_SESSION_ID": "sess-7"}, "claude:sess-7"),
            # harness ids are namespaced, so they can never look like a path or a slug
            ({"CODEX_THREAD_ID": "thread-9", "CLAUDE_CODE_SESSION_ID": "sess-7"}, "codex:thread-9"),
            ({}, cwd),
            # blank is not an identity: fall through instead of owning ""
            ({"CDX_OWNER": "  ", "CODEX_THREAD_ID": "thread-9"}, "codex:thread-9"),
            ({"CDX_OWNER": "", "CODEX_THREAD_ID": ""}, cwd),
        ):
            with self.subTest(env=env):
                saved = {key: os.environ.get(key) for key in ("CDX_OWNER", *(name for name, _ in cdx.HARNESS_SESSION_ENV))}
                try:
                    for key in saved:
                        os.environ.pop(key, None)
                    os.environ.update(env)
                    self.assertEqual(cdx.task_owner(), expected)
                finally:
                    for key, value in saved.items():
                        os.environ.pop(key, None)
                        if value is not None:
                            os.environ[key] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
