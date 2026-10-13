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
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import (
    ArchiveSettings, BackupOptions, CloudSettings, DirectoryScanSettings, ExcludePathEntry,
    JobCatalog, JobDefinition, RetentionSettings, ScheduleDefinition, TransferMonitorSettings,
    path_is_excluded_from_backup, split_remote_path,
)
from app.jobs_loader import load_catalog, save_catalog


def download(**kwargs):
    return replace(JobDefinition(
        key='cloud_myspace', order=1, description='Скачать MYSPACE', timeout_seconds=3600,
        enabled=True, continue_on_error=True, kind='backup', transfer_direction='download',
        source_path='mail:/MYSPACE', destination_path='/mnt/pve/hdd/cloud/MYSPACE',
        destination_mount='/mnt/pve/hdd',
    ), **kwargs)


def isolated_api():
    """Only definitions, never live main imports/startup/config or network IO."""
    from fastapi import HTTPException
    from pydantic import BaseModel, Field
    import app.domain as domain
    path=Path(__file__).resolve().parents[1]/'app'/'main.py'
    tree=ast.parse(path.read_text(encoding='utf-8'))
    names={'_job_definition_from_payload','_compose_cloud_destination','update_jobs','update_backups',
           'browse_cloud_directories'}
    nodes=[]
    for node in tree.body:
        if ((isinstance(node,ast.ClassDef) and any(isinstance(base,ast.Name) and base.id=='BaseModel' for base in node.bases))
                or isinstance(node,ast.FunctionDef) and node.name in names):
            node.decorator_list=[];nodes.append(node)
    ns=dict(vars(domain),BaseModel=BaseModel,Field=Field,HTTPException=HTTPException,Any=object)
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),ns)
    for node in nodes:
        if isinstance(node,ast.ClassDef):ns[node.name].model_rebuild(_types_namespace=ns)
    return ns


class DownloadJobTests(unittest.TestCase):
    def test_default_direction_upload_backward_compatible(self):
        job=replace(download(), transfer_direction='upload', source_path='/media/photo',
                    destination_path='mail:/PHOTO', destination_mount=None).validate()
        self.assertEqual(job.command[:4],['rclone','copy','/media/photo','mail:/PHOTO'])
        self.assertEqual(JobDefinition.__dataclass_fields__['transfer_direction'].default,'upload')
        with TemporaryDirectory() as temp:
            p=Path(temp)/'jobs.json'
            p.write_text(json.dumps({'jobs':[{'key':'old','command':['rclone','copy','/media/photo','mail:/PHOTO']}]}))
            loaded=load_catalog(p).get_job('old')
            self.assertEqual(loaded.transfer_direction,'upload')
            self.assertIsNone(loaded.source_subpath)
            self.assertIsNone(loaded.destination_mount)

    def test_persist_direction_cloud_source_mount_schedule_and_profiles(self):
        job=download(cloud_key='mail',source_subpath='MYSPACE',enabled=False,
                     schedule=ScheduleDefinition(enabled=True,mode='daily',hour=4),
                     transfer_monitor=TransferMonitorSettings(enabled=True),
                     options=BackupOptions(exclude_paths=[ExcludePathEntry('mail:/MYSPACE/BACKUPS/HA')]))
        with TemporaryDirectory() as temp:
            p=Path(temp)/'jobs.json';save_catalog(p,JobCatalog([job],{'standard':[job.key],'all':[job.key]}))
            stored=json.loads(p.read_text())['jobs'][0]
            self.assertEqual(stored['transfer_direction'],'download')
            self.assertEqual(stored['source_subpath'],'MYSPACE')
            self.assertEqual(stored['destination_mount'],'/mnt/pve/hdd')
            loaded=load_catalog(p).get_job(job.key)
            self.assertEqual(loaded,job.validate())
            self.assertIn(job.key,load_catalog(p).profiles['standard'])
            self.assertEqual(loaded.to_dict()['direction_label'],'Скачивание из облака')
            self.assertTrue(loaded.transfer_monitor.enabled)
            self.assertFalse(loaded.enabled)

    def test_three_download_commands_and_anchored_myspace_exclusions(self):
        paths=['Proxmox','projects','HA']
        for name in ['IMAGES','MUSIC','MYSPACE']:
            options=BackupOptions(exclude_paths=[ExcludePathEntry(f'mail:/MYSPACE/BACKUPS/{p}') for p in paths]) if name=='MYSPACE' else BackupOptions()
            job=download(source_path=f'mail:/{name}/',destination_path=f'/mnt/pve/hdd/cloud/{name}/',options=options).validate()
            self.assertEqual(job.command[:4],['rclone','copy',f'mail:/{name}',f'/mnt/pve/hdd/cloud/{name}'])
            self.assertNotIn('--delete-excluded',job.command)
            self.assertNotIn('delete',job.command)
            if name=='MYSPACE':
                excludes=[job.command[i+1] for i,p in enumerate(job.command) if p=='--exclude']
                self.assertEqual(excludes,[f'/BACKUPS/{p}/**' for p in paths])
                for p in paths:
                    self.assertTrue(path_is_excluded_from_backup(source_path=job.source_path,
                        target_path=f'mail:/MYSPACE/BACKUPS/{p}/archive', options=options))
                self.assertFalse(path_is_excluded_from_backup(source_path=job.source_path,
                    target_path='mail:/MYSPACE/Other/BACKUPS/Proxmox/a',options=options))

    def test_remote_paths_spaces_unicode_and_file_excludes_do_not_resolve_locally(self):
        source='mail:/MYSPACE/My Pictures/Фото'
        with patch.object(Path,'resolve',side_effect=AssertionError('Remote paths must be lexical')):
            self.assertEqual(split_remote_path(source+'/'),('mail','/MYSPACE/My Pictures/Фото'))
            job=download(source_path=source,options=BackupOptions(exclude_paths=[
                ExcludePathEntry(source+'/keep folder','directory'),
                ExcludePathEntry(source+'/cover [1].jpg','file')])).validate()
            args=job.options.to_args(source_path=job.source_path)
            self.assertIn('/keep folder/**',args)
            self.assertIn('/cover \\[1\\].jpg',args)
            self.assertTrue(path_is_excluded_from_backup(source_path=source,target_path=source+'/keep folder/pic',options=job.options))
        self.assertEqual(split_remote_path('mail:/Music/title: live.mp3'),('mail','/Music/title: live.mp3'))
        self.assertEqual(split_remote_path('mail:'),('mail','/'))
        for p in ['/local/path',':http:/path','mail:/A/../B','mail,token=secret:/path','C:\\windows','mail:/bad\x00path']:
            with self.subTest(p=p):self.assertIsNone(split_remote_path(p))

    def test_outside_or_wrong_remote_exclude_paths_rejected(self):
        for path in ['other:/MYSPACE/BACKUPS/HA','/local/BACKUPS/HA','mail:/MYSPACE2/BACKUPS/HA',
                     'mail:/MYSPACE/../IMAGES','mail:/MYSPACE']:
            with self.subTest(path=path),self.assertRaises(ValueError):
                download(options=BackupOptions(exclude_paths=[ExcludePathEntry(path)])).validate()

    def test_download_requires_remote_and_absolute_nonroot_local_dest_mount(self):
        for kwargs in [dict(source_path='/local/source'),dict(source_path='mail:MYSPACE'),
                       dict(source_path='mail:/MYSPACE/../Other'),dict(source_path=None),
                       dict(destination_path='/'),dict(destination_path='relative'),dict(destination_path='other:/dst'),
                       dict(destination_path='/mnt/pve/hdd/../root'),dict(destination_path=None),
                       dict(destination_mount='/'),dict(destination_mount='relative'),dict(destination_mount='mail:/dst'),
                       dict(destination_mount='/mnt/another'),dict(source_subpath='../MYSPACE'),
                       dict(source_subpath='mail:/MYSPACE'),dict(transfer_direction='unknown')]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):download(**kwargs).validate()
        self.assertIsNone(download(destination_mount=None).validate().destination_mount)
        self.assertEqual(download(source_subpath='/MYSPACE/').validate().source_subpath,'MYSPACE')

    def test_copy_only_and_no_cloud_cleanup_archive_watcher_or_date_scan(self):
        for kwargs in [dict(transfer_mode='sync'),dict(transfer_mode='invalid'),dict(kind='command'),
                       dict(archive=ArchiveSettings(enabled=True)),dict(retention=RetentionSettings(enabled=True,min_age='7d')),
                       dict(retention=RetentionSettings(enabled=True)),dict(watcher_enabled=True),
                       dict(directory_scan=DirectoryScanSettings(enabled=True))]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):download(**kwargs).validate()

    def test_destructive_or_link_advanced_options_rejected(self):
        for arg in ['--delete-excluded','--delete-before=false','--remove-source-files','--remove-source-dirs',
                    '--move','--purge','--rmdirs','--links','--copy-links','-L','--copy-unsafe-links',
                    '--config /tmp/other.conf','--backup-dir /root/escape','--suffix old']:
            with self.subTest(arg=arg),self.assertRaises(ValueError):
                download(options=BackupOptions(extra_args=[arg])).validate()
        job=download(options=BackupOptions(extra_args=['--checksum','--dry-run'],exclude=['*.tmp'])).validate()
        self.assertIn('--checksum',job.command)

    def test_custom_selectors_cannot_override_structured_download_exclusions(self):
        selectors=['--filter "+ /**"','-f "+ /**"','-f="+ /**"','--filter-from /tmp/rules',
                   '--include=/**','--include-from=/tmp/rules','--exclude=*.tmp',
                   '--exclude-from /tmp/rules','--exclude-if-present=.skip',
                   '--files-from /tmp/files','--files-from-raw=/tmp/files','--ignore-case',
                   '--max-age=7d','--min-age 1d','--download']
        for arg in selectors:
            with self.subTest(arg=arg), self.assertRaisesRegex(ValueError,'custom selectors'):
                download(options=BackupOptions(extra_args=[arg],exclude=['/BACKUPS/HA/**'])).validate()
        job=download(options=BackupOptions(max_age='7d',min_age='1h',exclude=['*.tmp'],
                     exclude_paths=[ExcludePathEntry('mail:/MYSPACE/BACKUPS/HA')],
                     extra_args=['--no-traverse','--checksum'])).validate()
        self.assertIn('--max-age',job.command)
        self.assertIn('--min-age',job.command)
        self.assertIn('/BACKUPS/HA/**',job.command)
        self.assertIn('*.tmp',job.command)
        self.assertIn('--no-traverse',job.command)

    def test_legacy_retention_migration_cannot_attach_to_download(self):
        job=download(schedule=ScheduleDefinition(enabled=True,mode='daily',hour=4))
        cleanup=JobDefinition(key='cleanup',order=2,description='Legacy',timeout_seconds=3600,enabled=True,
                              continue_on_error=True,kind='command',schedule=job.schedule,
                              command=['rclone','delete',job.destination_path,'--min-age','7d'])
        with TemporaryDirectory() as temp:
            p=Path(temp)/'jobs.json';save_catalog(p,JobCatalog([job,cleanup],{'standard':[job.key,cleanup.key]}))
            catalog=load_catalog(p)
            self.assertIsNotNone(catalog.get_job('cleanup'))
            self.assertFalse(catalog.get_job(job.key).retention.enabled)
            self.assertEqual(catalog.get_job(job.key).transfer_direction,'download')

    def test_api_both_endpoints_compose_source_cloud_not_destination(self):
        api=isolated_api()
        for endpoint,model in [('update_jobs','JobCatalogPayload'),('update_backups','BackupCatalogPayload')]:
            for cloud_root in ['', '/Root With Spaces/Фото']:
                with self.subTest(endpoint=endpoint,cloud_root=cloud_root):
                    cloud=CloudSettings(key='mail',title='Mail',remote_name='mail',root_path=cloud_root)
                    api.update(catalog=JobCatalog([],{},clouds=[cloud]),catalog_lock=threading.RLock(),
                               _refresh_catalog_clouds_from_rclone=lambda:[cloud],settings=SimpleNamespace(jobs_file=None),
                               save_catalog=Mock(),build_profiles=lambda jobs,**_:{'standard':[j.key for j in jobs]},
                               event_watcher=SimpleNamespace(sync_from_catalog=Mock()))
                    data=dict(key='download',transfer_direction='download',cloud_key='mail',source_subpath='MYSPACE',
                              destination_path='/mnt/pve/hdd/cloud/MYSPACE',destination_mount='/mnt/pve/hdd',
                              options=dict(exclude_paths=[dict(path=f'mail:{cloud_root}/MYSPACE/BACKUPS/HA',kind='directory')]))
                    result=api[endpoint](api[model](jobs=[data]));self.assertTrue(result['saved'])
                    job=api['catalog'].get_job('download')
                    self.assertEqual(job.source_path,f'mail:{cloud_root}/MYSPACE')
                    self.assertEqual(job.destination_path,'/mnt/pve/hdd/cloud/MYSPACE')
                    self.assertEqual(job.transfer_direction,'download')
                    self.assertIn('/BACKUPS/HA/**',job.command)
                    api['save_catalog'].assert_called_once()
                    # No cloud_key: explicit named remote source is still supported.
                    data.pop('cloud_key');data['source_path']=job.source_path
                    api[endpoint](api[model](jobs=[data]))
                    self.assertEqual(api['catalog'].get_job('download').source_path,job.source_path)

    def test_api_selected_cloud_root_download_uses_explicit_slash_upload_stays_legacy(self):
        api=isolated_api();cloud=CloudSettings(key='mail',title='Mail',remote_name='mail',root_path=None)
        for endpoint,model in [('update_jobs','JobCatalogPayload'),('update_backups','BackupCatalogPayload')]:
            with self.subTest(endpoint=endpoint):
                api.update(catalog=JobCatalog([],{},clouds=[cloud]),catalog_lock=threading.RLock(),
                           _refresh_catalog_clouds_from_rclone=lambda:[cloud],settings=SimpleNamespace(jobs_file=None),
                           save_catalog=Mock(),build_profiles=lambda jobs,**_:{'standard':[j.key for j in jobs]},
                           event_watcher=SimpleNamespace(sync_from_catalog=Mock()))
                api[endpoint](api[model](jobs=[dict(key='root',transfer_direction='download',cloud_key='mail',
                              source_subpath='',destination_path='/mnt/pve/hdd/cloud/root',destination_mount='/mnt/pve/hdd')]))
                self.assertEqual(api['catalog'].get_job('root').source_path,'mail:/')
                self.assertEqual(api['catalog'].get_job('root').command[:4],
                                 ['rclone','copy','mail:/','/mnt/pve/hdd/cloud/root'])
                api[endpoint](api[model](jobs=[dict(key='upload',cloud_key='mail',source_path='/media/photo',
                              destination_subpath='')]))
                self.assertEqual(api['catalog'].get_job('upload').destination_path,'mail:')

    def test_api_rejects_invalid_download_without_catalog_write(self):
        from fastapi import HTTPException
        api=isolated_api()
        for endpoint,model in [('update_jobs','JobCatalogPayload'),('update_backups','BackupCatalogPayload')]:
            api.update(catalog=JobCatalog([],{}),catalog_lock=threading.RLock(),_refresh_catalog_clouds_from_rclone=lambda:[],
                       settings=SimpleNamespace(jobs_file=None),save_catalog=Mock(),
                       build_profiles=lambda jobs,**_:{'standard':[j.key for j in jobs]},
                       event_watcher=SimpleNamespace(sync_from_catalog=Mock()))
            base=dict(key='d',transfer_direction='download',source_path='mail:/MYSPACE',destination_path='/mnt/pve/hdd/cloud/MYSPACE',destination_mount='/mnt/pve/hdd')
            for change in [dict(transfer_mode='sync'),dict(destination_path='/'),dict(destination_mount='/other'),
                           dict(cloud_key='missing'),dict(source_subpath='../MYSPACE'),
                           dict(options={'extra_args':['--remove-source-files']}),dict(watcher_enabled=True),
                           dict(retention={'enabled':True,'min_age':'7d'}),dict(archive={'enabled':True}),
                           dict(directory_scan={'enabled':True})]:
                with self.subTest(endpoint=endpoint,change=change):
                    api['save_catalog'].reset_mock()
                    with self.assertRaises(HTTPException) as raised:
                        api[endpoint](api[model](jobs=[dict(base,**change)]))
                    self.assertEqual(raised.exception.status_code,400)
                    api['save_catalog'].assert_not_called()
            if endpoint=='update_jobs':
                with self.assertRaises(HTTPException):
                    api[endpoint](api[model](jobs=[dict(base,kind='command',command=['true'])]))

    def test_cloud_browse_include_files_is_forwarded_without_extra_side_effects(self):
        api=isolated_api();cloud=CloudSettings(key='mail',title='Mail',remote_name='mail')
        api.update(catalog=JobCatalog([],{},clouds=[cloud]),_browse_cloud_directories=Mock(return_value={'files':[]}))
        self.assertEqual(api['browse_cloud_directories']('mail','MYSPACE',include_files=True),{'files':[]})
        api['_browse_cloud_directories'].assert_called_once_with(cloud.normalized(),'MYSPACE',include_files=True)


if __name__=='__main__':unittest.main()
