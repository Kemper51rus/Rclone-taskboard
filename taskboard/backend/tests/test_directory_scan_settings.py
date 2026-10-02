from __future__ import annotations

import ast
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import (
    ArchiveSettings, BackupOptions, DirectoryScanSettings, JobCatalog, JobDefinition,
    JobNotificationSettings, RetentionSettings, ScheduleDefinition, TransferMonitorSettings,
)
from app.jobs_loader import load_catalog, save_catalog


def backup(**kwargs):
    return replace(JobDefinition(
        key="backup", order=1, description="Backup", timeout_seconds=1800,
        enabled=True, continue_on_error=True, kind="backup", source_path="/tmp/source",
        destination_path="remote:backup",
    ), **kwargs)


def isolated_api():
    """Execute only model and endpoint definitions, never main's startup/config IO."""
    from fastapi import HTTPException
    from pydantic import BaseModel, Field
    import app.domain as domain

    path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"_job_definition_from_payload", "update_jobs", "update_backups"}
    nodes = []
    for node in tree.body:
        if (isinstance(node, ast.ClassDef) and any(
            isinstance(base, ast.Name) and base.id == "BaseModel" for base in node.bases
        )) or (isinstance(node, ast.FunctionDef) and node.name in names):
            node.decorator_list = []
            nodes.append(node)
    namespace = dict(vars(domain), BaseModel=BaseModel, Field=Field,
                     HTTPException=HTTPException, Any=object)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            namespace[node.name].model_rebuild(_types_namespace=namespace)
    return namespace


class DirectoryScanSettingsTests(unittest.TestCase):
    def test_defaults_and_legacy_jobs(self):
        expected = dict(enabled=False, path_template="%Y-%m-%d", lookback_days=7,
                        timezone="UTC", full_scan_enabled=True,
                        full_scan_interval_hours=168, full_scan_ignore_age=False)
        self.assertEqual(DirectoryScanSettings().to_dict(), expected)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "jobs.json"
            path.write_text(json.dumps({"jobs": [{"key": "old", "command": [
                "rclone", "copy", "/tmp/source", "remote:backup"
            ]}]}), encoding="utf-8")
            loaded = load_catalog(path).get_job("old")
            self.assertEqual(loaded.directory_scan.to_dict(), expected)
            self.assertEqual(loaded.to_dict()["directory_scan"], expected)

    def test_roundtrip_and_retention_migration_preserves_settings(self):
        scan = DirectoryScanSettings(enabled=True, path_template="tenant-*/%Y/%m/%d",
                                     lookback_days=30, timezone="Europe/Moscow",
                                     full_scan_enabled=False, full_scan_interval_hours=24,
                                     full_scan_ignore_age=True)
        job = backup(directory_scan=scan, watcher_enabled=True,
                     transfer_monitor=TransferMonitorSettings(enabled=True))
        legacy_retention = replace(job, key="cleanup", order=2, kind="command",
                                   directory_scan=DirectoryScanSettings(),
                                   command=["rclone", "delete", "remote:backup", "--min-age", "30d"])
        with TemporaryDirectory() as temp:
            path = Path(temp) / "jobs.json"
            save_catalog(path, JobCatalog([job, legacy_retention], {}))
            loaded = load_catalog(path)
            migrated = loaded.get_job("backup")
            self.assertIsNone(loaded.get_job("cleanup"))
            self.assertTrue(migrated.retention.enabled)
            self.assertEqual(migrated.directory_scan, scan)
            self.assertTrue(migrated.watcher_enabled)
            self.assertTrue(migrated.transfer_monitor.enabled)
            self.assertEqual(load_catalog(path).get_job("backup").directory_scan, scan)

    def test_disabled_draft_is_preserved_even_for_command(self):
        scan = DirectoryScanSettings(path_template="../bad", timezone="invalid", lookback_days=0,
                                     full_scan_interval_hours=0)
        job = backup(kind="command", command=["true"], directory_scan=scan)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "jobs.json"
            save_catalog(path, JobCatalog([job], {}))
            self.assertEqual(load_catalog(path).get_job("backup").directory_scan, scan)

    def test_invalid_templates(self):
        for template in ["", "/%Y-%m-%d", "../%Y-%m-%d", "./%Y-%m-%d",
                         "%Y//%m/%d", "%Y/%m/%d/", "%Y\\%m\\%d", "%Y-%m-%d\n",
                         "%Y-%m-%d/**", "%Y-%m-%d/[ab]", "%Y-%m-%d/{a,b}",
                         "%Y-%m-%d/?", "%Y-%m-%d/a b", "%Y-%m-%d/é", "C:/%Y-%m-%d",
                         "%Y-%m", "%Y-%m-%d/%H", "%Y-%m-%d/%%", "%Y-%m-%d/%",
                         "%Y-%m-%d/%-d", "%Y-%m-%d/" + "x" * 256]:
            with self.subTest(template=template), self.assertRaisesRegex(ValueError, "directory_scan"):
                DirectoryScanSettings(enabled=True, path_template=template).normalized()

    def test_ranges_and_timezone(self):
        for kwargs in [dict(lookback_days=0), dict(lookback_days=3651), dict(lookback_days=True),
                       dict(lookback_days=1.5), dict(full_scan_interval_hours=0),
                       dict(full_scan_interval_hours=87601), dict(timezone="Mars/Olympus"),
                       dict(timezone="/etc/passwd"), dict(timezone="")]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "directory_scan"):
                DirectoryScanSettings(enabled=True, **kwargs).normalized()
        for days, hours in [(1, 1), (3650, 87600)]:
            self.assertTrue(DirectoryScanSettings(enabled=True, lookback_days=days,
                                                full_scan_interval_hours=hours).normalized().enabled)

    def test_only_copy_nonarchive_backup(self):
        scan = DirectoryScanSettings(enabled=True)
        for kwargs in [dict(kind="command"), dict(transfer_mode="sync"),
                       dict(transfer_mode="invalid"), dict(archive=ArchiveSettings(enabled=True))]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "only.*copy"):
                backup(directory_scan=scan, **kwargs).validate()

    def test_conflicting_custom_args_but_structured_excludes_allowed(self):
        for arg in ["--delete-excluded", "--filter '+ foo'", "--include=foo", "--filter-from rules",
                    "--exclude=foo", "--files-from=paths", "--min-age 3d", "-f='+ foo'"]:
            with self.subTest(arg=arg), self.assertRaisesRegex(ValueError, "conflicts"):
                backup(directory_scan=DirectoryScanSettings(enabled=True),
                       options=BackupOptions(extra_args=[arg])).validate()
        backup(directory_scan=DirectoryScanSettings(enabled=True),
               options=BackupOptions(exclude=["*.tmp"], max_age="7d", extra_args=["--checksum"])).validate()

    def test_api_models_conversion_and_http400_without_live_config(self):
        from fastapi import HTTPException
        api = isolated_api()
        for endpoint, model in [("update_jobs", "JobCatalogPayload"),
                                ("update_backups", "BackupCatalogPayload")]:
            api.update(catalog=JobCatalog([], {}), catalog_lock=threading.RLock(),
                       _refresh_catalog_clouds_from_rclone=lambda: [],
                       _compose_cloud_destination=lambda *_: None,
                       settings=SimpleNamespace(jobs_file=None), save_catalog=Mock(),
                       build_profiles=lambda jobs, **_: {"standard": [job.key for job in jobs]},
                       event_watcher=SimpleNamespace(sync_from_catalog=Mock()))
            data = dict(key="api", source_path="/tmp/source", destination_path="remote:backup",
                        directory_scan=dict(enabled=True, timezone="UTC", lookback_days=14))
            payload = api[model](jobs=[data])
            result = api[endpoint](payload)
            self.assertTrue(result["saved"])
            self.assertEqual(api["catalog"].get_job("api").directory_scan.lookback_days, 14)
            self.assertEqual(api[model](jobs=[dict(data, directory_scan={})]).jobs[0]
                             .directory_scan.model_dump(), DirectoryScanSettings().to_dict())
            for scan in [dict(enabled=True, path_template="../%Y-%m-%d"),
                         dict(enabled=True, lookback_days=0), dict(enabled=True, timezone="invalid")]:
                with self.subTest(endpoint=endpoint, scan=scan):
                    api["save_catalog"].reset_mock()
                    with self.assertRaises(HTTPException) as raised:
                        api[endpoint](api[model](jobs=[dict(data, directory_scan=scan)]))
                    self.assertEqual(raised.exception.status_code, 400)
                    api["save_catalog"].assert_not_called()
            with self.assertRaises(HTTPException) as raised:
                api[endpoint](api[model](jobs=[dict(data, transfer_mode="sync")]))
            self.assertEqual(raised.exception.status_code, 400)
        with self.assertRaises(HTTPException) as raised:
            api["update_jobs"](api["JobCatalogPayload"](jobs=[dict(
                key="command", kind="command", command=["true"], directory_scan=dict(enabled=True)
            )]))
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
