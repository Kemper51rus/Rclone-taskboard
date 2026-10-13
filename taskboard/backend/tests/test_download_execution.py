from __future__ import annotations

from dataclasses import replace
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
from app.domain import BackupOptions, ExcludePathEntry, JobCatalog, JobDefinition, CloudSettings
from app.download_guard import validate_download_execution
from app.jobs_overlap import download_overlaps
from app.orchestrator import Orchestrator
from app.runner import CommandResult
from app.storage import Storage
from app.watcher import FilesystemWatcher
from app.cloud_browse import _browse_cloud_directories, _cloud_browse_remote_path


def download(destination, **kw):
    return JobDefinition(key='cloud_myspace', order=1, description='Download MYSPACE',
                         timeout_seconds=3600, enabled=True, continue_on_error=True, kind='backup',
                         source_path='mail:/MYSPACE', destination_path=str(destination),
                         transfer_direction='download', **kw).validate()


def upload(key, remote, enabled=True):
    return JobDefinition(key=key, order=1, description=key, timeout_seconds=60,
                         enabled=enabled, continue_on_error=True, kind='backup',
                         source_path='/local', destination_path=remote).validate()


class DownloadExecutionTests(unittest.TestCase):
    def test_mount_guard_existing_parent_without_creating_destination(self):
        with TemporaryDirectory() as temp:
            dest=Path(temp)/'cloud'/'MYSPACE';job=download(dest,destination_mount=temp)
            with patch('app.download_guard.os.path.ismount',return_value=True):
                validate_download_execution(job,job.command+['--bwlimit','10M','--log-file','/tmp/test.log'])
            self.assertFalse(dest.exists())
            with patch('app.download_guard.os.path.ismount',return_value=False), self.assertRaisesRegex(ValueError,'mount'):
                validate_download_execution(job,job.command)

    def test_symlink_file_full_disk_and_readonly_fail_closed(self):
        with TemporaryDirectory() as temp:
            base=Path(temp);(base/'elsewhere').mkdir();(base/'link').symlink_to(base/'elsewhere',target_is_directory=True)
            job=download(base/'link'/'cloud')
            with self.assertRaisesRegex(ValueError,'symlink'):validate_download_execution(job,job.command)
            (base/'file').write_text('fixture');job=download(base/'file'/'cloud')
            with self.assertRaisesRegex(ValueError,'directory'):validate_download_execution(job,job.command)
            job=download(base/'cloud')
            with patch('app.download_guard.os.access',return_value=False),self.assertRaisesRegex(ValueError,'writable'):
                validate_download_execution(job,job.command)
            with patch('app.download_guard.os.statvfs',return_value=SimpleNamespace(f_bavail=0,f_frsize=4096)),self.assertRaisesRegex(ValueError,'disk space'):
                validate_download_execution(job,job.command)

    def test_stale_roots_and_exclusions_cannot_execute(self):
        with TemporaryDirectory() as temp:
            job=download(Path(temp)/'cloud',options=BackupOptions(exclude=['/BACKUPS/Proxmox/**']))
            stale=replace(job,options=BackupOptions()).validate().command
            with self.assertRaisesRegex(ValueError,'exclusions'):validate_download_execution(job,stale)
            for cmd in [job.command[:2]+['mail:/OTHER',job.destination_path]+job.command[4:],
                        ['rclone','sync',*job.command[2:]],job.command+['--remove-source-files']]:
                with self.assertRaises(ValueError):validate_download_execution(job,cmd)
            old=replace(job,transfer_direction='upload')
            with self.assertRaises(ValueError):validate_download_execution(old,job.command)

    def test_non_transfer_command_jobs_are_unaffected(self):
        command_job = JobDefinition(key='command',order=1,description='command',timeout_seconds=60,
                                    enabled=True,continue_on_error=True,kind='command').validate()
        for argv in [['rclone','size','mail:/MYSPACE','--json'],
                     ['rclone','delete','mail:/trash','--min-age','7d'],
                     ['rclone','lsjson','mail:/MYSPACE','--dirs-only']]:
            validate_download_execution(command_job,argv)
        with TemporaryDirectory() as temp:
            job=download(Path(temp)/'cloud')
            for argv in [['python3','-m','app.archive_job','--source','/tmp'],['rclone','size','mail:/MYSPACE']]:
                with self.assertRaises(ValueError):validate_download_execution(job,argv)

    def test_uploads_are_unaffected_and_download_no_retention_or_watcher(self):
        with TemporaryDirectory() as temp:
            job=download(Path(temp)/'cloud')
            validate_download_execution(upload('photo','mail:/PHOTO'),['rclone','copy','/src','mail:/PHOTO'])
            store=Storage(Path(temp)/'db');store.initialize()
            catalog=JobCatalog([job],{'standard':[job.key]})
            orchestrator=Orchestrator(SimpleNamespace(enable_scheduler=False),store,catalog,None,None)
            self.assertEqual(len(orchestrator._expand_steps([job])),1)
            self.assertEqual(FilesystemWatcher(catalog=catalog,on_event=lambda p:None)._collect_watch_entries(),[])

    def test_guard_failure_finishes_run_without_spawning_rclone(self):
        with TemporaryDirectory() as temp:
            job=download(Path(temp)/'cloud',destination_mount=temp)
            store=Storage(Path(temp)/'db');store.initialize();catalog=JobCatalog([job],{'standard':[job.key]})
            runner=SimpleNamespace(dry_run=False,run=lambda **kw:self.fail('rclone must not start'))
            orchestrator=Orchestrator(SimpleNamespace(enable_scheduler=False,default_timeout_seconds=60,app_root=Path(temp)),store,catalog,runner,None)
            rid=store.create_run('standard','manual','test','test',{});store.insert_run_steps(rid,orchestrator._expand_steps([job]))
            with patch.object(orchestrator,'_step_needs_copy_gate',return_value=False),patch.object(orchestrator,'_step_rclone_log_mode',return_value=None),\
                 patch.object(orchestrator,'_step_transfer_metrics',return_value={}),patch.object(orchestrator,'_update_job_auto_rclone_log_state'),\
                 patch.object(orchestrator,'_notify_for_step'),patch('app.download_guard.os.path.ismount',return_value=False):
                orchestrator._process_run(rid,'standard')
            self.assertEqual(store.get_run(rid)['status'],'failed')
            self.assertIn('mount',store.list_run_steps(rid)[0]['stderr_tail'])
            self.assertFalse((Path(temp)/'cloud').exists())

    def test_overlap_disabled_uploads_boundaries_and_coverage(self):
        with TemporaryDirectory() as temp:
            job=download(Path(temp)/'cloud',options=BackupOptions(exclude=['/BACKUPS/Proxmox/**'],exclude_paths=[
                ExcludePathEntry('mail:/MYSPACE/BACKUPS/HA','directory')]))
            jobs=[job,upload('pve','mail:/MYSPACE/BACKUPS/Proxmox'),upload('ha','mail:/MYSPACE/BACKUPS/HA',False),
                  upload('projects','mail:/MYSPACE/BACKUPS/projects',False),upload('photo','mail:/PHOTO'),
                  upload('otherremote','other:/MYSPACE/BACKUPS/Proxmox'),upload('similar','mail:/MYSPACE2')]
            overlaps=download_overlaps(job,jobs)
            self.assertEqual({o['job_key'] for o in overlaps},{'pve','ha','projects'})
            self.assertTrue(next(o for o in overlaps if o['job_key']=='pve')['covered'])
            self.assertTrue(next(o for o in overlaps if o['job_key']=='ha')['covered'])
            self.assertFalse(next(o for o in overlaps if o['job_key']=='projects')['covered'])
            allover=download_overlaps(job,[upload('whole','mail:/')])[0]
            self.assertTrue(allover['entire_source']);self.assertEqual(allover['exclude_pattern'],'/**')

    def test_cloud_exclude_browser_lists_files_and_cannot_escape_root(self):
        cloud=CloudSettings(key='mail',title='mail',remote_name='mail',root_path='/MYSPACE')
        completed=SimpleNamespace(returncode=0,stdout=json.dumps([{'Name':'BACKUPS','IsDir':True},{'Name':'notes.txt','IsDir':False,'Size':123}]),stderr='')
        with patch('app.cloud_browse.subprocess.run',return_value=completed) as run:
            result=_browse_cloud_directories(cloud,'',include_files=True)
            self.assertNotIn('--dirs-only',run.call_args.args[0])
            self.assertEqual(result['files'][0]['remote_path'],'mail:/MYSPACE/notes.txt')
            self.assertEqual(result['directories'][0]['path'],'BACKUPS')
        with self.assertRaises(ValueError):_cloud_browse_remote_path(cloud,'../other')

    @unittest.skipUnless(shutil.which('rclone'),'requires rclone')
    def test_real_remote_to_local_copy_respects_exclusions_without_deleting_source(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source'/'MYSPACE';destination=root/'download'/'MYSPACE'
            for relative in ['DOCUMENTS/keep.txt','BACKUPS/Proxmox/skip.bin','BACKUPS/projects/skip.bin','BACKUPS/HA/skip.bin']:
                p=source/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(relative)
            config=root/'rclone.conf';config.write_text('[fixture]\ntype = local\n')
            job=download(destination,options=BackupOptions(exclude=['/BACKUPS/Proxmox/**','/BACKUPS/projects/**','/BACKUPS/HA/**']))
            environment={**os.environ,'RCLONE_CONFIG':str(config)}
            job=replace(job,source_path='fixture:'+source.as_posix()).validate()
            validate_download_execution(job,job.command)
            subprocess.run(job.command,capture_output=True,text=True,timeout=20,check=True,env=environment)
            self.assertTrue((destination/'DOCUMENTS/keep.txt').is_file())
            self.assertFalse((destination/'BACKUPS').exists())
            self.assertTrue((source/'BACKUPS/Proxmox/skip.bin').is_file())
            (destination/'local-only.txt').write_text('local backup')
            subprocess.run(job.command,capture_output=True,text=True,timeout=20,check=True,env=environment)
            self.assertTrue((destination/'local-only.txt').exists())

if __name__=='__main__':unittest.main()
