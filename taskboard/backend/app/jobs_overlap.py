"""Read-only overlap hints for cloud-to-disk tasks.

Include disabled upload definitions: they are still managed paths and may be
re-enabled later. These are configuration hints, not a scan of cloud contents.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .domain import JobDefinition, split_remote_path


def _covers_branch(pattern: str, relative: str) -> bool:
    # Conservatively mark a branch covered only for explicit whole-tree rules.
    # Arbitrary user globs may be useful but should not hide overlap warnings.
    candidate = pattern.strip().lstrip('/')
    branch = relative.strip('/')
    if candidate in {'**', '**/*'}:
        return True
    if not candidate.endswith('/**'):
        return False
    root = candidate[:-3].rstrip('/')
    return root == branch or (root and branch.startswith(root + '/'))


def download_overlaps(job: JobDefinition, jobs: list[JobDefinition]) -> list[dict[str, Any]]:
    if job.kind != 'backup' or job.transfer_direction != 'download':
        return []
    source = split_remote_path(job.source_path)
    if source is None:
        return []
    remote, source_path = source
    source_root = PurePosixPath(source_path)
    args = job.options.to_args(transfer_mode='copy', source_path=job.source_path)
    patterns = [args[i + 1] for i, token in enumerate(args[:-1]) if token == '--exclude']
    output = []
    for upload in jobs:
        if upload.key == job.key or upload.kind != 'backup' or upload.transfer_direction != 'upload':
            continue
        target = split_remote_path(upload.destination_path)
        if target is None or target[0] != remote:
            continue
        upload_root = PurePosixPath(target[1])
        try:
            relative = upload_root.relative_to(source_root).as_posix()
            entire_source = relative == '.'
        except ValueError:
            try:
                source_root.relative_to(upload_root)
            except ValueError:
                continue
            relative = '.'
            entire_source = True
        pattern = '/**' if entire_source else f'/{relative}/**'
        output.append({
            'job_key': upload.key, 'title': upload.title or upload.description or upload.key,
            'enabled': upload.enabled, 'cloud_path': upload.destination_path,
            'relative_path': relative, 'exclude_pattern': pattern,
            'covered': any(_covers_branch(p, '' if entire_source else relative) for p in patterns),
            'entire_source': entire_source,
        })
    return sorted(output, key=lambda item: (item['relative_path'], item['job_key']))
