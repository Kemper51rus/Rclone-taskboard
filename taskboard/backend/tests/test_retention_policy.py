from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.domain import JobCatalog, JobDefinition, RetentionSettings, RetentionDirectoryScanSettings
from app.orchestrator import Orchestrator
from app.retention_policy import (build_retention_plan, command_is_dry_run, compatible_full_cleanup, retention_state_key,
                                  successful_state, policy_status)
from app.runner import CommandResult, CommandRunner
from app.storage import Storage

NOW = datetime(2026, 10, 3, 0, 20, tzinfo=timezone.utc)


def job_for(*, interval=True, scan=True, **scan_options):
    return JobDefinition(key='frigate', order=1, description='Frigate', timeout_seconds=3600,
                         enabled=True, continue_on_error=True, kind='backup', source_path='/src',
                         destination_path='remote:/dst', retention=RetentionSettings(
                             enabled=True, min_age='7d', interval_enabled=interval, interval_hours=24,
                             exclude=['**/keep/**'], directory_scan=RetentionDirectoryScanSettings(
                                 enabled=scan, **scan_options))).validate()


def command_for(job):
    return JobDefinition.build_retention_command(job.destination_path, job.retention)


def old_state():
    return {'last_cleanup_started_at': '2026-10-01T23:00:00+00:00',
            'last_success_at': '2026-10-01T23:15:00+00:00',
            'last_full_scan_at': '2026-09-30T23:15:00+00:00'}


class RetentionPolicyTests(unittest.TestCase):
    def test_interval_skips_and_legacy_always_runs(self):
        job = job_for(); state = old_state(); state['last_success_at'] = (NOW - timedelta(hours=1)).isoformat()
        plan = build_retention_plan(job, command_for(job), now=NOW, state=state)
        self.assertEqual(plan.mode, 'skip_interval')
        self.assertEqual(plan.next_due_at, (NOW + timedelta(hours=23)).isoformat())
        job = job_for(interval=False, scan=False)
        self.assertEqual(build_retention_plan(job, command_for(job), now=NOW, state=state).mode, 'full')

    def test_first_cleanup_full_baseline_and_due_full_audit_keep_age(self):
        job = job_for()
        for state in [{}, {**old_state(), 'last_full_scan_at':'2000-01-01T00:00:00+00:00'}]:
            plan = build_retention_plan(job, command_for(job), now=NOW, state=state)
            self.assertEqual(plan.mode, 'full')
            self.assertEqual(plan.command, command_for(job))
            self.assertEqual(plan.command[plan.command.index('--min-age') + 1], '7d')

    def test_expired_band_includes_partial_cutoff_and_overlap_not_fresh_dates(self):
        job = job_for()
        plan = build_retention_plan(job, command_for(job), now=NOW, state=old_state())
        self.assertEqual(plan.mode, 'window')
        self.assertEqual(plan.paths, ['2026-09-23', '2026-09-24', '2026-09-25', '2026-09-26'])
        self.assertNotIn('+ /2026-09-27/**', plan.command)
        self.assertEqual(plan.command[plan.command.index('--filter')+1], '- **/keep/**')
        self.assertEqual(plan.command[-2:], ['--filter','- /**'])
        self.assertIn('--min-age', plan.command)
        self.assertNotIn('--purge', plan.command)
        self.assertNotIn('--rmdirs', plan.command)

    def test_start_watermark_not_finish_prevents_long_cleanup_gap(self):
        job = job_for(interval=False, full_scan_enabled=False, overlap_days=0)
        state = {**old_state(), 'last_cleanup_started_at':'2026-09-25T00:00:00+00:00',
                 'last_success_at':'2026-10-02T23:59:00+00:00'}
        plan = build_retention_plan(job, command_for(job), now=NOW, state=state)
        self.assertEqual(plan.paths[0], '2026-09-18')
        self.assertEqual(plan.paths[-1], '2026-09-26')

    def test_timezone_dst_nested_mask_and_large_gap(self):
        job = job_for(interval=False, full_scan_enabled=False, path_template='cameras/*/%Y/%m/%d', timezone='Europe/Moscow')
        plan = build_retention_plan(job, command_for(job), now=NOW, state=old_state())
        self.assertEqual(plan.paths[-1], 'cameras/*/2026/09/26')
        state = {**old_state(), 'last_cleanup_started_at':'2000-01-01T00:00:00+00:00'}
        self.assertEqual(build_retention_plan(job, command_for(job), now=NOW, state=state).mode, 'full')
        berlin = job_for(interval=False, full_scan_enabled=False, timezone='Europe/Berlin', overlap_days=0)
        state = {'last_cleanup_started_at':'2026-04-04T23:30:00+00:00',
                 'last_success_at':'2026-04-04T23:40:00+00:00','last_full_scan_at':'2026-04-04T23:40:00+00:00'}
        plan = build_retention_plan(berlin, command_for(berlin), now=datetime(2026,4,5,23,30,tzinfo=timezone.utc),state=state)
        self.assertEqual(plan.paths,['2026-03-29','2026-03-30'])

    def test_disabled_failed_copy_and_stale_command_fail_closed(self):
        job = job_for()
        plan = build_retention_plan(job, command_for(job), now=NOW, state={}, copy_succeeded=False)
        self.assertEqual(plan.mode, 'skip_copy_failed')
        disabled = replace(job,retention=replace(job.retention, enabled=False))
        self.assertEqual(build_retention_plan(disabled, command_for(job), now=NOW, state={}).mode, 'skip_disabled')
        for args in [['--filter','+ /**'],['--min-age','0s'],['--delete-excluded']]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                build_retention_plan(job, command_for(job)+args, now=NOW, state=old_state())

    def test_adopt_only_same_unfiltered_real_success(self):
        job = job_for(); cmd = command_for(job)
        step = {'status':'succeeded','step_kind':'retention', 'started_at': '2026-10-01T23:00:00+00:00',
                'finished_at':'2026-10-01T23:15:00+00:00', 'command':cmd+['--bwlimit','1250000B','--log-file','/tmp/log']}
        baseline = compatible_full_cleanup(job, step)
        self.assertEqual(baseline['last_cleanup_started_at'], step['started_at'])
        self.assertEqual(baseline['last_full_scan_at'], step['finished_at'])
        for changed in [dict(step,status='failed'),dict(step,command=cmd+['--filter','- /**']),
                        dict(step,command=cmd+['--dry-run']),dict(step,stdout_tail='dry-run: rclone delete')]:
            self.assertIsNone(compatible_full_cleanup(job, changed))
        changed_job=replace(job,retention=replace(job.retention,min_age='14d'))
        self.assertIsNone(compatible_full_cleanup(changed_job, step))

    def test_interval_changes_keep_clock_and_success_watermark(self):
        job = job_for(); changed = replace(job,retention=replace(job.retention,interval_hours=48))
        self.assertEqual(retention_state_key(job),retention_state_key(changed))
        plan=build_retention_plan(job,command_for(job),now=NOW,state=old_state())
        updated=successful_state(plan,old_state(),NOW+timedelta(hours=2))
        self.assertEqual(updated['last_cleanup_started_at'], NOW.isoformat())
        self.assertEqual(updated['last_full_scan_at'],old_state()['last_full_scan_at'])
        self.assertEqual(policy_status(job,updated)['next_due_at'],(NOW+timedelta(hours=26)).isoformat())

    def test_orchestrator_skip_success_failure_and_dry_run(self):
        for status,dry_run,due in [('succeeded',False,True),('failed',False,True),('succeeded',True,True),('succeeded',False,False)]:
            with self.subTest(status=status,dry_run=dry_run,due=due), TemporaryDirectory() as temp:
                store=Storage(Path(temp)/'db');store.initialize();job=job_for()
                catalog=JobCatalog([job],{'standard':[job.key]})
                calls=[]
                def run_command(**kwargs):
                    calls.append(kwargs['command'])
                    return CommandResult(status if kwargs['command'][1]=='delete' else 'succeeded',0,'','',1)
                runner=SimpleNamespace(dry_run=dry_run,run=run_command)
                orchestrator=Orchestrator(SimpleNamespace(enable_scheduler=False,default_timeout_seconds=60,app_root=Path(temp)),store,catalog,runner,None)
                run=store.create_run('standard','manual','test','test',{})
                store.insert_run_steps(run,orchestrator._expand_steps([job]))
                key=retention_state_key(job)
                state={'last_cleanup_started_at':(datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),
                       'last_success_at':(datetime.now(timezone.utc)-timedelta(days=2) if due else datetime.now(timezone.utc)).isoformat(),
                       'last_full_scan_at':datetime.now(timezone.utc).isoformat()}
                store.set_state(key,json.dumps(state))
                with patch.object(orchestrator,'_step_needs_copy_gate',return_value=False), \
                     patch.object(orchestrator,'_step_rclone_log_mode',return_value=None), \
                     patch.object(orchestrator,'_step_transfer_metrics',return_value={}), \
                     patch.object(orchestrator,'_update_job_auto_rclone_log_state'), \
                     patch.object(orchestrator,'_notify_for_step'):
                    orchestrator._process_run(run,'standard')
                after=json.loads(store.get_state(key))
                if due and status=='succeeded' and not dry_run:
                    self.assertNotEqual(after['last_success_at'],state['last_success_at'])
                else:self.assertEqual(after,state)
                steps=store.list_run_steps(run)
                self.assertEqual(steps[1]['status'],'skipped' if not due else status)
                self.assertEqual(len(calls),2 if due else 1)
                self.assertEqual(store.get_run(run)['status'],'failed' if due and status=='failed' else 'succeeded')

    def test_parallel_workers_execute_one_due_cleanup(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        with TemporaryDirectory() as temp:
            store=Storage(Path(temp)/'db');store.initialize();job=job_for()
            catalog=JobCatalog([job],{'standard':[job.key]})
            both_copies=threading.Barrier(2);calls=[];guard=threading.Lock()
            def execute(**kwargs):
                if kwargs['command'][1]=='copy':both_copies.wait(timeout=5)
                else:
                    with guard:calls.append(kwargs['command'])
                return CommandResult('succeeded',0,'','',1)
            runner=SimpleNamespace(dry_run=False,run=execute)
            orchestrator=Orchestrator(SimpleNamespace(enable_scheduler=False,default_timeout_seconds=60,app_root=Path(temp)),store,catalog,runner,None)
            runs=[]
            for _ in range(2):
                rid=store.create_run('standard','manual','test','test',{})
                store.insert_run_steps(rid,orchestrator._expand_steps([job]));runs.append(rid)
            with patch.object(orchestrator,'_step_needs_copy_gate',return_value=False), \
                 patch.object(orchestrator,'_step_rclone_log_mode',return_value=None), \
                 patch.object(orchestrator,'_step_transfer_metrics',return_value={}), \
                 patch.object(orchestrator,'_update_job_auto_rclone_log_state'), \
                 patch.object(orchestrator,'_notify_for_step'), ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(orchestrator._process_run,rid,'standard') for rid in runs]
                for future in futures:future.result(timeout=10)
            self.assertEqual(len(calls),1)
            self.assertEqual(sorted(store.list_run_steps(rid)[1]['status'] for rid in runs),['skipped','succeeded'])
            self.assertTrue(all(store.get_run(rid)['status']=='succeeded' for rid in runs))

    def test_inactive_bad_scan_draft_has_readable_status(self):
        job=job_for()
        job=replace(job,retention=replace(job.retention,enabled=False,directory_scan=replace(
            job.retention.directory_scan,path_template='../invalid',timezone='invalid',overlap_days=-1))).validate()
        self.assertTrue(retention_state_key(job).startswith('retention_policy:'))
        self.assertFalse(policy_status(job,{})['enabled'])
        with TemporaryDirectory() as temp:
            store=Storage(Path(temp)/'db');store.initialize()
            orchestrator=Orchestrator(SimpleNamespace(enable_scheduler=False),store,
                                      JobCatalog([job],{'standard':[job.key]}),None,None)
            self.assertFalse(orchestrator.retention_status(job.key)['enabled'])

    def test_short_dry_run_flags_cannot_seed_or_advance_cleanup(self):
        for flag in ['-n=true','-nv','-vn','--dry-run']:
            with self.subTest(flag=flag), TemporaryDirectory() as temp:
                job=job_for();job=replace(job,retention=replace(job.retention,extra_args=[flag])).validate()
                cmd=command_for(job)
                self.assertTrue(command_is_dry_run(cmd))
                self.assertIsNone(compatible_full_cleanup(job,{'status':'succeeded','step_kind':'retention',
                    'command':cmd,'started_at':NOW.isoformat(),'finished_at':NOW.isoformat()}))
                store=Storage(Path(temp)/'db');store.initialize()
                orchestrator=Orchestrator(SimpleNamespace(enable_scheduler=False,default_timeout_seconds=60,app_root=Path(temp)),
                    store,JobCatalog([job],{'standard':[job.key]}),
                    SimpleNamespace(dry_run=False,run=lambda **kw:CommandResult('succeeded',0,'','',1)),None)
                rid=store.create_run('standard','manual','test','test',{})
                store.insert_run_steps(rid,orchestrator._expand_steps([job]))
                state={'last_cleanup_started_at':(datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),
                       'last_success_at':(datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),
                       'last_full_scan_at':datetime.now(timezone.utc).isoformat()}
                key=retention_state_key(job);store.set_state(key,json.dumps(state))
                with patch.object(orchestrator,'_step_needs_copy_gate',return_value=False), \
                     patch.object(orchestrator,'_step_rclone_log_mode',return_value=None), \
                     patch.object(orchestrator,'_step_transfer_metrics',return_value={}), \
                     patch.object(orchestrator,'_update_job_auto_rclone_log_state'), \
                     patch.object(orchestrator,'_notify_for_step'):
                    orchestrator._process_run(rid,'standard')
                self.assertEqual(json.loads(store.get_state(key)),state)
        self.assertFalse(command_is_dry_run(['rclone','delete','remote:/dst','--filter','- **/Entrance/**','--exclude','-notes','--min-age','7d']))
        self.assertFalse(command_is_dry_run(['rclone','delete','remote:/dst','-n=false']))

    def test_listing_and_deletion_progress_counters(self):
        progress=CommandRunner._parse_progress_line('Checks: 0 / 0, -, Listed 13473')
        self.assertEqual(progress['listed_count'],13473)
        self.assertEqual(CommandRunner._parse_progress_line('Deleted: 2 (files), 0 (dirs), 3.318 MiB (freed)')['deleted_count'],2)

    @unittest.skipUnless(shutil.which('rclone'),'requires rclone')
    def test_real_rclone_only_deletes_old_mtime_in_selected_band(self):
        now=datetime.now(timezone.utc)
        with TemporaryDirectory() as temp:
            root=Path(temp)/'cloud';root.mkdir()
            old_day=(now-timedelta(days=8)).strftime('%Y-%m-%d')
            outside=(now-timedelta(days=100)).strftime('%Y-%m-%d')
            fixtures=[(f'{old_day}/old.mp4',True),(f'{old_day}/fresh.mp4',False),
                      (f'{old_day}/keep/old.mp4',True),(f'{outside}/old.mp4',True)]
            for rel,old in fixtures:
                p=root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture')
                t=(now-timedelta(days=200)).timestamp() if old else now.timestamp();os.utime(p,(t,t))
            job=replace(job_for(interval=False),destination_path=str(root)).validate()
            state={'last_cleanup_started_at':(now-timedelta(days=1)).isoformat(),
                   'last_success_at':(now-timedelta(hours=23)).isoformat(),'last_full_scan_at':(now-timedelta(hours=23)).isoformat()}
            plan=build_retention_plan(job,command_for(job),now=now,state=state)
            dry_command=list(plan.command)
            log_index=dry_command.index('--log-level');del dry_command[log_index:log_index+2]
            # -v is incompatible with an explicit --log-level, so isolate flag
            # syntax without that unrelated conflict (only our temporary files).
            for flag in ['-n=true','-nv','-vn']:
                subprocess.run(dry_command+[flag],capture_output=True,text=True,timeout=20,check=True)
                self.assertTrue((root/old_day/'old.mp4').exists())
            result=subprocess.run(plan.command,capture_output=True,text=True,timeout=20,check=True)
            self.assertFalse((root/old_day/'old.mp4').exists())
            self.assertTrue((root/old_day/'fresh.mp4').exists())
            self.assertTrue((root/old_day/'keep/old.mp4').exists())
            self.assertTrue((root/outside/'old.mp4').exists())
            self.assertTrue((root/old_day).is_dir()) # no purge/rmdirs
            full=build_retention_plan(job,command_for(job),now=now,state={})
            subprocess.run(full.command,capture_output=True,text=True,timeout=20,check=True)
            self.assertFalse((root/outside/'old.mp4').exists())
            self.assertTrue((root/old_day/'fresh.mp4').exists())
            self.assertTrue((root/old_day/'keep/old.mp4').exists())

if __name__=='__main__':unittest.main()
