from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.directory_scan import build_scan_plan, directory_paths, state_key_for_job
from app.domain import BackupOptions, DirectoryScanSettings, JobCatalog, JobDefinition, RetentionSettings
from app.orchestrator import Orchestrator
from app.runner import CommandResult
from app.storage import Storage

NOW = datetime(2026, 10, 2, 22, 30, tzinfo=timezone.utc)


def make_job(**scan):
    return JobDefinition(key='frigate', order=1, description='Frigate', timeout_seconds=3600,
                         enabled=True, continue_on_error=True, kind='backup',
                         source_path='/src/', destination_path='remote:/dst',
                         options=BackupOptions(max_age='7d'),
                         directory_scan=DirectoryScanSettings(enabled=True, **scan)).validate()


class DirectoryScanTests(unittest.TestCase):
    def test_window_includes_partial_boundary_day(self):
        job = make_job()
        self.assertEqual(directory_paths(job, NOW), [f'2026-09-{n}' for n in range(25, 31)] + ['2026-10-01', '2026-10-02'])
        self.assertEqual(directory_paths(make_job(timezone='Europe/Moscow'), NOW)[-1], '2026-10-03')

    def test_nested_wildcard_template(self):
        job = make_job(path_template='cameras/*/%Y/%m/%d', lookback_days=1)
        self.assertEqual(directory_paths(job, NOW), ['cameras/*/2026/10/01', 'cameras/*/2026/10/02'])

    def test_exclusions_precede_date_includes_and_fast_list_removed(self):
        job = replace(make_job(), options=BackupOptions(max_age='7d', exclude=['**/private/**'], fast_list=True)).validate()
        plan = build_scan_plan(job, job.command, now=NOW, anchor_at=None)
        self.assertEqual(plan.mode, 'window')
        self.assertNotIn('--fast-list', plan.command)
        self.assertNotIn('--exclude', plan.command)
        self.assertEqual(plan.command[plan.command.index('--filter') + 1], '- **/private/**')
        self.assertEqual(plan.command[-2:], ['--filter', '- /**'])
        self.assertIn('--max-age', plan.command)

    def test_full_scan_due_keeps_user_age_limits_unless_opted_out(self):
        job = make_job(full_scan_interval_hours=24)
        before = (NOW - timedelta(hours=23, minutes=59)).isoformat()
        self.assertEqual(build_scan_plan(job, job.command, now=NOW, anchor_at=before).mode, 'window')
        due = (NOW - timedelta(hours=24)).isoformat()
        full = build_scan_plan(job, job.command, now=NOW, anchor_at=due)
        self.assertEqual(full.mode, 'full')
        self.assertEqual(full.command, job.command)
        job = make_job(full_scan_interval_hours=24, full_scan_ignore_age=True)
        full = build_scan_plan(job, job.command + ['--min-age=10m'], now=NOW, anchor_at=due)
        self.assertNotIn('--max-age', full.command)
        self.assertNotIn('--min-age=10m', full.command)

    def test_disabled_periodic_scan_and_invalid_clock_state(self):
        job = make_job(full_scan_enabled=False)
        self.assertEqual(build_scan_plan(job, job.command, now=NOW, anchor_at='2000-01-01T00:00:00+00:00').mode, 'window')
        job = make_job()
        for anchor in [None, 'invalid', '2026-10-02T22:30:00', (NOW + timedelta(days=1)).isoformat()]:
            self.assertEqual(build_scan_plan(job, job.command, now=NOW, anchor_at=anchor).anchor_at, NOW.isoformat())

    def test_configuration_identity_and_mismatched_queued_root(self):
        job = make_job()
        self.assertNotEqual(state_key_for_job(job), state_key_for_job(replace(job, destination_path='remote:/other')))
        command = list(job.command); command[2] = '/other/'
        with self.assertRaises(ValueError):
            build_scan_plan(job, command, now=NOW, anchor_at=None)

    def test_stale_queued_selection_cannot_override_pruning(self):
        job = make_job()
        for args in [['--filter', '+ /**'], ['--include=/**'], ['-f+ /**'], ['--files-from', '/tmp/files']]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                build_scan_plan(job, job.command + args, now=NOW, anchor_at=None)

    def test_orchestrator_records_plan_and_retention_is_unchanged(self):
        with TemporaryDirectory() as temp:
            store = Storage(Path(temp) / 'db'); store.initialize()
            job = replace(make_job(), retention=RetentionSettings(enabled=True, min_age='7d'))
            catalog = JobCatalog([job], {'standard': [job.key]})
            orchestrator = Orchestrator(SimpleNamespace(enable_scheduler=False), store, catalog,
                                        SimpleNamespace(dry_run=False), None)
            run = store.create_run('standard', 'manual', 'test', 'test', {})
            definitions = orchestrator._expand_steps([job])
            store.insert_run_steps(run, definitions)
            first, retention = store.list_run_steps(run)
            plan = orchestrator._prepare_directory_scan(first)
            self.assertEqual(store.get_state(plan.state_key), plan.anchor_at)
            self.assertEqual(store.get_run_step(first['id'])['command'], plan.command)
            self.assertEqual(store.get_run_step(first['id'])['progress']['directory_scan']['mode'], 'window')
            self.assertIsNone(orchestrator._prepare_directory_scan(retention))
            self.assertEqual(store.get_run_step(retention['id'])['command'], definitions[1].command)
            # State survives creating another Storage instance / service restart.
            self.assertEqual(Storage(store.db_path).get_state(plan.state_key), plan.anchor_at)

    def test_full_scan_anchor_updates_only_on_real_success(self):
        for result_status, dry_run in [('failed', False), ('succeeded', True), ('succeeded', False)]:
            with self.subTest(result_status=result_status, dry_run=dry_run), TemporaryDirectory() as temp:
                store = Storage(Path(temp) / 'db'); store.initialize()
                job = make_job(full_scan_interval_hours=1)
                catalog = JobCatalog([job], {'standard': [job.key]})
                runner = SimpleNamespace(dry_run=dry_run, run=lambda **kw: CommandResult(result_status, 0, '', '', 1))
                orchestrator = Orchestrator(SimpleNamespace(enable_scheduler=False, default_timeout_seconds=60), store, catalog, runner, None)
                run = store.create_run('standard', 'manual', 'test', 'test', {})
                store.insert_run_steps(run, orchestrator._expand_steps([job]))
                key = state_key_for_job(job); anchor = '2000-01-01T00:00:00+00:00'; store.set_state(key, anchor)
                with patch.object(orchestrator, '_step_needs_copy_gate', return_value=False), \
                     patch.object(orchestrator, '_step_execution_context', return_value=__import__('contextlib').nullcontext()), \
                     patch.object(orchestrator, '_step_rclone_log_mode', return_value=None), \
                     patch.object(orchestrator, '_step_transfer_metrics', return_value={}), \
                     patch.object(orchestrator, '_update_job_auto_rclone_log_state'), \
                     patch.object(orchestrator, '_notify_for_step'):
                    orchestrator._process_run(run, 'standard')
                self.assertEqual(store.get_state(key) != anchor, result_status == 'succeeded' and not dry_run)

    @unittest.skipUnless(shutil.which('rclone'), 'rclone integration requires rclone')
    def test_real_rclone_prunes_old_branches_and_preserves_exclusions(self):
        with TemporaryDirectory() as temp:
            root = Path(temp) / 'src'; destination = Path(temp) / 'dst'
            for rel in ['2026-06-29/old.mp4', '2026-10-02/allowed.mp4', '2026-10-02/private/secret.mp4', 'outside.mp4']:
                path = root / rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('fixture')
            job = replace(make_job(lookback_days=1), source_path=str(root), destination_path=str(destination),
                          options=BackupOptions(exclude=['**/private/**'])).validate()
            plan = build_scan_plan(job, job.command, now=NOW, anchor_at=None)
            command = ['rclone', 'lsf', str(root), '--recursive', '--files-only', '--log-level', 'DEBUG']
            for i, token in enumerate(plan.command):
                if token == '--filter': command.extend([token, plan.command[i + 1]])
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True)
            self.assertEqual(result.stdout.splitlines(), ['2026-10-02/allowed.mp4'])
            self.assertIn('2026-06-29: Excluded', result.stderr)
            self.assertIn('2026-10-02/private: Excluded', result.stderr)
            self.assertNotIn('2026-06-29/old.mp4', result.stderr)
            # Nested masks also infer ancestor directory rules without + /**/.
            job = replace(job, directory_scan=replace(job.directory_scan, path_template='cameras/*/%Y/%m/%d'))
            nested = root / 'cameras' / 'one' / '2026' / '10' / '02' / 'clip.mp4'
            nested.parent.mkdir(parents=True); nested.write_text('fixture')
            plan = build_scan_plan(job, job.command, now=NOW, anchor_at=None)
            command = ['rclone', 'lsf', str(root), '--recursive', '--files-only']
            for i, token in enumerate(plan.command):
                if token == '--filter': command.extend([token, plan.command[i + 1]])
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True)
            self.assertEqual(result.stdout.splitlines(), ['cameras/one/2026/10/02/clip.mp4'])


if __name__ == '__main__':
    unittest.main()
