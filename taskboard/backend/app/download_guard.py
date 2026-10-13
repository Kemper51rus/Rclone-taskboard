"""Preflight checks for remote-to-local backup tasks, without creating files."""
from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any

from .domain import JobDefinition, split_remote_path


def validate_download_execution(job: JobDefinition | None, command: list[str]) -> None:
    configured_download = job is not None and job.transfer_direction == 'download'
    transfer_command = (len(command) >= 4 and command[0] == 'rclone'
                        and command[1] in {'copy', 'copyto', 'sync', 'move', 'moveto'})
    # Other rclone subcommands have only one path; their next token is a flag,
    # not a local destination. Preserve existing command jobs such as size/delete.
    if not transfer_command:
        if configured_download:
            raise ValueError('Queued download is not a copy command; enqueue the task again')
        return
    remote_source = split_remote_path(command[2])
    remote_destination = split_remote_path(command[3])
    actual_download = remote_source is not None and remote_destination is None
    if not actual_download and not configured_download:
        return
    if not job or job.kind != 'backup' or not configured_download:
        raise ValueError('Remote-to-local execution requires an explicit download task; enqueue it again')
    job = job.validate()
    def semantic_args(argv: list[str]) -> list[str]:
        output = []
        index = 0
        while index < len(argv):
            token = argv[index]
            if token in {'--bwlimit', '--log-file'}:
                index += 2
            elif token.split('=', 1)[0] in {'--bwlimit', '--log-file'}:
                index += 1
            else:
                output.append(token)
                index += 1
        return output
    if semantic_args(command) != semantic_args(job.command) or command[:2] != ['rclone', 'copy']:
        raise ValueError('Queued download paths/direction/exclusions differ from current settings; enqueue the task again')
    # Reject stale flags that disappeared from the current validated config.
    unsafe = {'--delete-excluded', '--remove-source-files', '--links', '--copy-links'}
    if any(token.split('=', 1)[0] in unsafe for token in command[4:]):
        raise ValueError('Queued download contains unsafe deletion/link flags')
    destination = Path(job.destination_path)
    # A symlinked local destination can silently redirect a backup elsewhere.
    # Check ancestors without resolving away the evidence and without mkdir.
    nearest = None
    for component in [*reversed(destination.parents), destination]:
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f'Cannot inspect download destination: {exc}') from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f'Download destination contains a symlink: {component}')
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f'Download destination is not a directory: {component}')
        nearest = component
    mount = job.destination_mount
    if mount:
        mount_path = Path(mount)
        if not os.path.ismount(mount_path):
            raise ValueError(f'Download destination mount is unavailable: {mount}; refusing to write to the system disk')
        try:
            destination.relative_to(mount_path)
        except ValueError as exc:
            raise ValueError('Download destination must be inside the configured mount') from exc
    if nearest is None or not os.access(nearest, os.W_OK | os.X_OK):
        raise ValueError('Download destination is not writable')
    try:
        space = os.statvfs(nearest)
    except OSError as exc:
        raise ValueError(f'Cannot read free space for download destination: {exc}') from exc
    if space.f_bavail * space.f_frsize <= 0:
        raise ValueError('Download destination has no available disk space')
