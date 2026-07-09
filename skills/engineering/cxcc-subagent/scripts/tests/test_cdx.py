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


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


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
        expected = {"medium": "low", "high": "medium", "max": "xhigh"}
        for model in ("fable", "claude-fable-5", "claude-fable-5-20260701"):
            for cdx_effort, provider_effort in expected.items():
                self.assertEqual(cdx.resolve_execution("claude", cdx_effort, model), (model, provider_effort))
        self.assertEqual(cdx.resolve_execution("claude", "high", "sonnet"), ("sonnet", "high"))
        self.assertEqual(cdx.resolve_execution("claude", "max", "opus"), ("opus", "xhigh"))

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
        # cdx "max" maps to grok "high"
        self.assertEqual(resume[resume.index("--reasoning-effort") + 1], "high")

    def test_grok_resume_without_thread_id_errors(self):
        prompt = self.base / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        with self.assertRaises(cdx.CdxError) as ctx:
            cdx.backend_cmd(self.grok_meta(), prompt, "resume", "grok-bin")
        self.assertEqual(ctx.exception.code, 4)

    def test_grok_effort_mapping(self):
        self.assertEqual(cdx.BACKENDS["grok"].efforts, {"medium": "medium", "high": "high", "max": "high"})
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
        with self.assertRaises(SystemExit):
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

        status = self.run_cdx(["status", "--json", "--state-dir", str(state), "race-task"])
        self.assertEqual(status.returncode, 10, status.stderr)
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
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        return subprocess.run(
            [sys.executable, str(CDX), *args],
            cwd=cwd or self.base,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
        )

    def test_watchdog_marks_fake_process_stalled(self):
        fake = make_fake_codex(self.base)
        state = self.base / "state"
        repo = self.base / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        env = {"CDX_CODEX_BIN": str(fake)}
        spawn = self.run_cdx(
            ["spawn", "--json", "--state-dir", str(state), "-C", str(repo), "--name", "stall-test", "--stall-after", "1", "STALL_MODE"],
            env=env,
        )
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        json.loads(spawn.stdout)
        deadline = time.time() + 90
        last = None
        stalled = False
        while time.time() < deadline:
            last = self.run_cdx(["status", "--json", "--state-dir", str(state), "stall-test"], env=env)
            data = json.loads(last.stdout)
            if data["state"] == "stalled":
                self.assertEqual(last.returncode, 12)
                stalled = True
                break
            time.sleep(0.5)
        if not stalled:
            self.fail(f"task did not stall; last={last.returncode if last else None} {last.stdout if last else ''} {last.stderr if last else ''}")
        # stalled tasks resume via plain send — no --now gate (SKILL.md: `send "continue"`)
        send = self.run_cdx(["send", "--json", "--state-dir", str(state), "stall-test", "continue"], env=env)
        self.assertEqual(send.returncode, 0, send.stderr)
        result = self.run_cdx(["result", "--json", "--state-dir", str(state), "stall-test", "--wait", "--timeout", "60"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resumed done", json.loads(result.stdout)["message"])


class CliSubprocessTests(TempCase):
    def run_cdx(self, args, env=None, cwd=None):
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
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
            status = self.run_cdx(["status", "--json", "--state-dir", str(state), "cli-task"], env=env)
            data = self.assert_json_stdout(status)
            if data["state"] == "done":
                break
            time.sleep(0.2)
        self.assertIsNotNone(status)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(data["model"], "gpt-5.6-sol")
        self.assertEqual(data["provider_effort"], "medium")

        listing = self.run_cdx(["list", "--json", "--state-dir", str(state), "--all"], env=env)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        listing_data = self.assert_json_stdout(listing)
        self.assertIsInstance(listing_data, list)
        self.assertEqual(listing_data[0]["model"], "gpt-5.6-sol")
        self.assertEqual(listing_data[0]["provider_effort"], "medium")

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
            status = self.run_cdx(["status", "--json", "--state-dir", str(state), "cli-task"], env=env)
            data = self.assert_json_stdout(status)
            if data["state"] == "done":
                break
            time.sleep(0.2)
        self.assertEqual(status.returncode, 0, status.stderr)

        kill = self.run_cdx(["kill", "--json", "--state-dir", str(state), "cli-task"], env=env)
        self.assertEqual(kill.returncode, 0, kill.stderr)
        # spec: kill on an already-terminal task is a strict no-op — state is reported, not rewritten
        self.assertEqual(self.assert_json_stdout(kill)["state"], "done")
        status_after = self.run_cdx(["status", "--json", "--state-dir", str(state), "cli-task"], env=env)
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
            status = self.run_cdx(["status", "--json", "--state-dir", str(state), "question-task"], env=env)
            data = self.assert_json_stdout(status)
            if data["state"] == "awaiting_reply":
                self.assertEqual(status.returncode, 11)
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
        self.assertIn("invalid choice", result.stderr)
        for choice in ("medium", "high", "max"):
            self.assertIn(choice, result.stderr)

    def test_spawn_help_explains_model_tiers_and_effort_dial(self):
        other = self.base / "other"
        other.mkdir()
        result = self.run_cdx(["spawn", "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sol|terra", result.stdout)
        self.assertIn("uniform across backends", result.stdout)
        self.assertIn("Fable override", result.stdout)
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


class RealBackendSmokeTests(TempCase):
    def run_cdx(self, args, env=None, cwd=None, timeout=120):
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
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
            last = self.run_cdx(["status", "--json", "--state-dir", str(state), task], timeout=30)
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
        status = self.run_cdx(["status", "--json", "--state-dir", str(state), "real-question"], timeout=30)
        self.assertEqual(status.returncode, 11, status.stderr)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
