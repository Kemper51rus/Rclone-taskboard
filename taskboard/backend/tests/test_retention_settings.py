from __future__ import annotations

import ast
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.domain import (
    ArchiveSettings, DirectoryScanSettings, JobCatalog, RetentionDirectoryScanSettings,
    RetentionSettings, parse_retention_age,
)
from app.jobs_loader import load_catalog, save_catalog
from test_directory_scan_settings import backup, isolated_api


def masked(**kwargs):
    return RetentionSettings(enabled=True, min_age='7d',
                             directory_scan=RetentionDirectoryScanSettings(enabled=True, **kwargs))


class RetentionSettingsTests(unittest.TestCase):
    def test_defaults_preserve_legacy_each_copy(self):
        retention = RetentionSettings().normalized()
        self.assertFalse(retention.interval_enabled)
        self.assertEqual(retention.interval_hours, 24)
        self.assertEqual(retention.directory_scan.to_dict(), {
            'enabled': False, 'path_template': '%Y-%m-%d', 'timezone': 'UTC',
            'overlap_days': 1, 'full_scan_enabled': True, 'full_scan_interval_hours': 168,
        })
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'jobs.json'
            path.write_text(json.dumps({'jobs': [dict(key='legacy', kind='backup',
                source_path='/tmp/source', destination_path='remote:backup',
                retention={'enabled': True, 'min_age': '7d'})]}))
            job = load_catalog(path).get_job('legacy')
            self.assertTrue(job.retention.enabled)
            self.assertFalse(job.retention.interval_enabled)
            self.assertFalse(job.retention.directory_scan.enabled)
            self.assertEqual(job.build_retention_command(job.destination_path, job.retention)[-2:], ['--min-age', '7d'])

    def test_positive_duration_parser(self):
        for value, duration in [('1s', timedelta(seconds=1)), ('0.5m', timedelta(seconds=30)),
                                ('.5h', timedelta(minutes=30)), ('1d12h', timedelta(hours=36)),
                                ('2w3d4h5m6.5s', timedelta(weeks=2, days=3, hours=4, minutes=5, seconds=6.5)),
                                (' 7d ', timedelta(days=7)), ('0s1m', timedelta(minutes=1))]:
            with self.subTest(value=value):
                self.assertEqual(parse_retention_age(value), duration)

    def test_duration_parser_rejects_undefined_absolute_or_nonpositive(self):
        for value in [None, '', '0', '0s', '-1d', 'off', 'inf', 'infinite', 'NaNd', '2026-01-01',
                      '2026-01-01T00:00:00Z', '1y', '1ms', '1D', '1d 2h', '1dgarbage', '1e3s',
                      True, '9' * 256 + 'w', '0.00000000000001s']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_retention_age(value)

    def test_interval_validation_only_when_active(self):
        for hours in [0, 87601, True, 1.5, '24']:
            with self.subTest(hours=hours), self.assertRaisesRegex(ValueError, 'interval_hours'):
                RetentionSettings(enabled=True, min_age='7d', interval_enabled=True, interval_hours=hours).normalized()
        for hours in [1, 87600]:
            self.assertEqual(RetentionSettings(enabled=True, min_age='7d', interval_enabled=True,
                                               interval_hours=hours).normalized().interval_hours, hours)
        self.assertEqual(RetentionSettings(interval_enabled=True, interval_hours=0).normalized().interval_hours, 0)
        self.assertEqual(RetentionSettings(enabled=True, min_age='7d', interval_hours=0).normalized().interval_hours, 0)

    def test_scan_uses_same_template_timezone_validator_as_copying(self):
        for template in ['', '/%Y-%m-%d', '../%Y-%m-%d', '%Y//%m/%d', '%Y/./%m/%d',
                         '%Y/%m/%d/', '%Y-%m', '%Y-%m-%d/%H', '%Y-%m-%d/**', '%Y-%m-%d/?',
                         '%Y-%m-%d/a b', '%Y-%m-%d/[ab]', '%Y-%m-%d/{a,b}', '%Y-%m-%d/é',
                         '%Y-%m-%d/' + 'x' * 256]:
            with self.subTest(template=template), self.assertRaisesRegex(ValueError, 'retention.directory_scan'):
                masked(path_template=template).normalized()
        for template in ['%Y-%m-%d', 'cameras/*/%Y/%m/%d', 'cam..era/%Y-%m-%d']:
            DirectoryScanSettings(enabled=True, path_template=template).normalized()
            self.assertEqual(masked(path_template=template).normalized().directory_scan.path_template, template)
        with self.assertRaisesRegex(ValueError, 'retention.directory_scan.timezone'):
            masked(timezone='Mars/Olympus').normalized()

    def test_overlap_and_full_scan_period_bounds(self):
        for kwargs in [dict(overlap_days=-1), dict(overlap_days=3651), dict(overlap_days=True),
                       dict(overlap_days=1.5), dict(full_scan_interval_hours=0), dict(full_scan_interval_hours=87601)]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                masked(**kwargs).normalized()
        for overlap, hours in [(0, 1), (3650, 87600)]:
            retention = masked(overlap_days=overlap, full_scan_interval_hours=hours).normalized()
            self.assertEqual(retention.directory_scan.overlap_days, overlap)

    def test_disabled_full_scan_preserves_inactive_period_draft(self):
        for hours in [0, -1, 87601, True, 1.5, 'invalid']:
            with self.subTest(hours=hours):
                scan = masked(full_scan_enabled=False, full_scan_interval_hours=hours).normalized().directory_scan
                self.assertEqual(scan.full_scan_interval_hours, hours)
                self.assertEqual(scan.to_dict()['full_scan_interval_hours'], hours)
                with self.assertRaises(ValueError):
                    masked(full_scan_enabled=True, full_scan_interval_hours=hours).normalized()

    def test_active_mask_requires_age_and_preserves_filters_in_command(self):
        for age in [None, '', '0s', '-1d', '2026-01-01']:
            with self.subTest(age=age), self.assertRaisesRegex(ValueError, 'retention.min_age'):
                replace(masked(), min_age=age).normalized()
        retention = replace(masked(), exclude=['*.keep'], extra_args=['--dry-run']).normalized()
        command = backup().build_retention_command('remote:backup', retention)
        self.assertIn('--min-age', command)
        self.assertEqual(command[command.index('--min-age') + 1], '7d')
        self.assertIn('--exclude', command)
        self.assertNotIn('--filter', command)  # Parent planner applies execution-time masks.

    def test_conflicting_custom_arguments_are_rejected(self):
        for arg in ['--filter +/**', '--filter-from file', '--include=/**', '--exclude *.keep',
                    '--exclude-from file', '--files-from file', '--files-from-raw file', '-f+/**',
                    '--min-age=1d', '--max-age 1d', '--max-depth=1', '--delete-excluded', '--rmdirs']:
            with self.subTest(arg=arg), self.assertRaisesRegex(ValueError, 'retention.directory_scan'):
                replace(masked(), extra_args=[arg]).normalized()
        self.assertTrue(replace(masked(), exclude=['*.keep'], extra_args=['--dry-run']).normalized().enabled)

    def test_scan_only_for_backup_copy(self):
        for kwargs in [dict(kind='command', command=['true']), dict(transfer_mode='sync'),
                       dict(transfer_mode='invalid')]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'only.*copy'):
                backup(retention=masked(), **kwargs).validate()
        # Archive-copy retention is permitted: mask must match the destination layout.
        backup(retention=masked(), archive=ArchiveSettings(enabled=True)).validate()

    def test_nested_dict_normalization_roundtrip_and_disabled_drafts(self):
        retention = RetentionSettings(enabled=True, min_age='7d', interval_enabled=True, interval_hours=48,
            directory_scan={'enabled': True, 'path_template': 'camera-*/%Y/%m/%d',
                            'timezone': 'Europe/Moscow', 'overlap_days': 2,
                            'full_scan_enabled': False, 'full_scan_interval_hours': 336})
        normalized = retention.normalized()
        self.assertIsInstance(normalized.directory_scan, RetentionDirectoryScanSettings)
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'jobs.json'
            save_catalog(path, JobCatalog([backup(retention=retention)], {}))
            self.assertEqual(load_catalog(path).get_job('backup').retention, normalized)
            stored = json.loads(path.read_text())['jobs'][0]['retention']
            self.assertEqual(stored['directory_scan']['overlap_days'], 2)
            self.assertNotIn('retention_status', stored)
            draft = RetentionSettings(enabled=False, interval_enabled=True, interval_hours=0,
                directory_scan={'enabled': True, 'path_template': '../bad', 'timezone': 'invalid',
                                'overlap_days': -1, 'full_scan_interval_hours': 0})
            save_catalog(path, JobCatalog([backup(retention=draft)], {}))
            self.assertEqual(load_catalog(path).get_job('backup').retention, draft.normalized())
        for scan in [None, [], 'bad', {'unsupported': True}]:
            with self.subTest(scan=scan), self.assertRaises(ValueError):
                RetentionSettings(directory_scan=scan).normalized()

    def test_both_api_payloads_roundtrip_and_http400_without_mutation(self):
        from fastapi import HTTPException
        api = isolated_api()
        for endpoint, model in [('update_jobs', 'JobCatalogPayload'), ('update_backups', 'BackupCatalogPayload')]:
            api.update(catalog=JobCatalog([], {}), catalog_lock=threading.RLock(),
                _refresh_catalog_clouds_from_rclone=lambda: [], _compose_cloud_destination=lambda *_: None,
                settings=SimpleNamespace(jobs_file=None), save_catalog=Mock(),
                build_profiles=lambda jobs, **_: {'standard': [j.key for j in jobs]},
                event_watcher=SimpleNamespace(sync_from_catalog=Mock()))
            data = dict(key='api', source_path='/tmp/source', destination_path='remote:backup',
                retention=dict(enabled=True, min_age='7d', interval_enabled=True, interval_hours=48,
                               directory_scan=dict(enabled=True, overlap_days=2)))
            result = api[endpoint](api[model](jobs=[data]))
            self.assertTrue(result['saved'])
            retained = api['catalog'].get_job('api').retention
            self.assertEqual(retained.interval_hours, 48)
            self.assertEqual(retained.directory_scan.overlap_days, 2)
            self.assertIsInstance(retained.directory_scan, RetentionDirectoryScanSettings)
            for retention in [dict(data['retention'], interval_hours=0),
                              dict(data['retention'], min_age='2026-01-01'),
                              dict(data['retention'], directory_scan=dict(enabled=True, path_template='../bad')),
                              dict(data['retention'], directory_scan=dict(enabled=True, overlap_days=-1)),
                              dict(data['retention'], extra_args=['--rmdirs'])]:
                api['save_catalog'].reset_mock()
                with self.subTest(endpoint=endpoint, retention=retention), self.assertRaises(HTTPException) as raised:
                    api[endpoint](api[model](jobs=[dict(data, retention=retention)]))
                self.assertEqual(raised.exception.status_code, 400)
                api['save_catalog'].assert_not_called()
            with self.assertRaises(HTTPException) as raised:
                api[endpoint](api[model](jobs=[dict(data, transfer_mode='sync')]))
            self.assertEqual(raised.exception.status_code, 400)
        with self.assertRaises(HTTPException) as raised:
            api['update_jobs'](api['JobCatalogPayload'](jobs=[dict(key='cmd', kind='command', command=['true'],
                retention=dict(enabled=True, min_age='7d', directory_scan=dict(enabled=True)))]))
        self.assertEqual(raised.exception.status_code, 400)

    def test_legacy_retention_command_migration_preserves_new_policy(self):
        settings = RetentionSettings(interval_enabled=True, interval_hours=48,
                                     directory_scan=RetentionDirectoryScanSettings(enabled=True, overlap_days=2))
        job = backup(retention=settings)
        cleanup = replace(job, key='legacy-cleanup', order=2, kind='command',
                          retention=RetentionSettings(), command=['rclone', 'delete', 'remote:backup', '--min-age', '7d'])
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'jobs.json'
            save_catalog(path, JobCatalog([job, cleanup], {}))
            catalog = load_catalog(path)
            self.assertIsNone(catalog.get_job('legacy-cleanup'))
            retention = catalog.get_job('backup').retention
            self.assertTrue(retention.enabled)
            self.assertTrue(retention.interval_enabled)
            self.assertEqual(retention.interval_hours, 48)
            self.assertEqual(retention.directory_scan, settings.directory_scan)
            self.assertEqual(retention.min_age, '7d')

    def test_runtime_status_is_read_only_annotation_not_catalog_data(self):
        from typing import Any
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'app/main.py').read_text())
        helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_jobs_with_retention_status')
        getter = Mock(return_value={'last_success_at': None, 'next_due_at': None})
        namespace = {'Any': Any, 'orchestrator': SimpleNamespace(retention_status=getter)}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), '<status-helper>', 'exec'), namespace)
        items = [backup().to_dict(), dict(key='command', kind='command')]
        result = namespace['_jobs_with_retention_status'](items)
        self.assertIn('retention_status', result[0])
        self.assertNotIn('retention_status', result[1])
        getter.assert_called_once_with('backup')


if __name__ == '__main__':
    unittest.main()
