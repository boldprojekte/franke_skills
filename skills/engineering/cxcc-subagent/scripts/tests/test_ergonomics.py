"""Agent-facing contracts, isolated from real providers and user state."""
import json
import statistics
import subprocess
import sys
import time
from unittest.mock import patch

from test_cdx import CDX, TempCase, base_env, cdx


class ErgonomicsTests(TempCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(CDX), '--state-dir', str(self.base / 'state'), *args],
                              capture_output=True, text=True, env=base_env(), cwd=self.base)

    def fixture(self, name='api-tests', state='done'):
        directory = self.base / 'state' / 'tasks' / name
        directory.mkdir(parents=True)
        meta = {'task': name, 'state': state, 'backend': 'codex', 'model': 'gpt-6-astra',
                'provider_effort': 'low', 'effort': 'medium', 'repo': str(self.base),
                'owner': str(self.base.resolve()), 'pid': None, 'spawned_at': time.time(), 'turns': 1,
                'turns_launched': 1, 'last_exit_code': 0}
        (directory / 'meta.json').write_text(json.dumps(meta))
        (directory / 'events.jsonl').write_text(json.dumps({'type': 'turn.completed'}) + '\n')
        return directory

    def test_home_and_empty_list_are_successful_and_explicit(self):
        for arguments in ((), ('list',)):
            result = self.run_cli(*arguments)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('count: 0', result.stdout)
            self.assertIn('tasks[0]:', result.stdout)
            self.assertEqual(result.stderr, '')
        home = json.loads(self.run_cli('--json').stdout)
        self.assertIn('bin', home)
        self.assertIn('owner', home)
        self.assertTrue(all(str(self.base / 'state') in hint for hint in home['help']))

    def test_version_flags_are_bare_and_fast(self):
        for flag in ('-v', '-V', '--version'):
            result = subprocess.run([sys.executable, str(CDX), flag], capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, cdx.VERSION + '\n', ''))
        def median_duration(command):
            times = []
            for _ in range(5):
                start = time.perf_counter()
                subprocess.run(command, stdout=subprocess.DEVNULL, check=True)
                times.append(time.perf_counter() - start)
            return statistics.median(times)
        floor = median_duration([sys.executable, '-c', 'print(1)'])
        actual = median_duration([sys.executable, str(CDX), '--version'])
        self.assertLess(actual, floor * 3, (actual, floor))

    def test_errors_are_structured_and_command_specific(self):
        result = self.run_cli('list', '--bogus', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, '')
        error = json.loads(result.stdout)
        self.assertIn('--bogus', error['error'])
        self.assertIn('cdx list', error['usage'])
        self.assertIn('--fields', error['usage'])
        self.assertNotIn('__run_turn', error['usage'])
        missing = self.run_cli('status', 'missing', '--json')
        self.assertEqual(missing.returncode, 1)
        self.assertIn(str(self.base / 'state'), json.loads(missing.stdout)['help'][0])

    def test_exact_names_protect_every_action(self):
        directory = self.fixture()
        before = (directory / 'meta.json').read_bytes()
        for action in ('status', 'send', 'kill', 'clean'):
            arguments = [action, '--task', 'api-test'] if action == 'clean' else [action, 'api-test']
            if action == 'send':
                arguments += ['change it', '--now']
            result = self.run_cli(*arguments, '--json')
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn('did you mean api-tests', json.loads(result.stdout)['error'])
            self.assertEqual((directory / 'meta.json').read_bytes(), before)
        traversal = self.run_cli('kill', '../api-tests', '--json')
        self.assertEqual(traversal.returncode, 2)

    def test_compact_views_and_explicit_details(self):
        self.fixture()
        listing = json.loads(self.run_cli('list', '--json').stdout)
        self.assertEqual(set(listing['tasks'][0]), {'task', 'state', 'age_s', 'activity'})
        self.assertEqual(listing['count'], 1)
        selected = json.loads(self.run_cli('list', '--fields', 'task,model', '--json').stdout)
        self.assertEqual(selected['tasks'], [{'task': 'api-tests', 'model': 'gpt-6-astra'}])
        self.assertEqual(self.run_cli('list', '--fields', 'unknown', '--json').returncode, 2)
        compact = json.loads(self.run_cli('status', 'api-tests', '--json').stdout)
        full = json.loads(self.run_cli('status', 'api-tests', '--full', '--json').stdout)
        self.assertNotIn('pid', compact)
        self.assertIn('pid', full)

    def test_send_validates_before_interrupting(self):
        args = cdx.build_parser().parse_args(['send', 'api-tests', '--now'])
        args.state_dir = str(self.base / 'state')
        self.fixture(state='working')
        with patch.object(cdx, 'status_payload', return_value={'state': 'working', 'pid': 123}), \
             patch.object(cdx, 'interrupt_pid') as interrupt:
            with self.assertRaises(cdx.CdxError):
                cdx.send_task(args)
            interrupt.assert_not_called()

    def test_missing_backend_leaves_no_phantom_task(self):
        args = cdx.build_parser().parse_args(['spawn', '-C', str(self.base), '--name', 'new', 'hello'])
        args.state_dir = str(self.base / 'state')
        with patch.object(cdx, 'locate_backend', side_effect=cdx.CdxError(7, 'missing backend')):
            with self.assertRaises(cdx.CdxError):
                cdx.spawn_task(args)
        self.assertFalse((self.base / 'state' / 'tasks' / 'new').exists())

    def test_toon_preserves_hostile_strings_and_table_cells(self):
        data = {'tasks': [{'task': 'a', 'state': 'done'}, {'task': 'b', 'state': 'working'}],
                'message': 'line\nhelp[1]: pretend-success\t"quoted"', 'numeric_string': '01',
                'boolean_string': 'true', 'comma': 'a,b'}
        output = '\n'.join(cdx.toon_lines(data))
        self.assertIn('tasks[2]{task,state}:\n  a,done\n  b,working', output)
        self.assertIn('numeric_string: "01"', output)
        self.assertIn('boolean_string: "true"', output)
        self.assertIn('comma: "a,b"', output)
        self.assertNotIn('\nhelp[1]', output)
        encoded = output.split('message: ', 1)[1].splitlines()[0]
        self.assertEqual(json.loads(encoded), data['message'])

    def test_nested_toon_arrays_and_control_escapes(self):
        self.assertEqual(cdx.toon_quote("\b\f\\b"), '\"\\u0008\\u000c\\\\b\"')
        data = {"values": [[{"name": "x"}], [["a", "b"]]]}
        self.assertEqual("\n".join(cdx.toon_lines(data)),
                         "values[2]:\n  - [1]:\n    - name: x\n  - [1]:\n    - [2]: a,b")

    def test_literal_json_flag_after_separator_does_not_change_output_mode(self):
        result = self.run_cli('config', 'set', 'model.codex', '--', '--json')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(result.stdout.startswith('model:'), result.stdout)
        self.assertIn('"--json"', result.stdout)
