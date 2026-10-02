"""Date-directory selection without walking the source filesystem.

Generated include rules prune whole directory branches in rclone. Full scans
keep user filters unless explicitly configured to ignore the file-age limits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

from .domain import JobDefinition


@dataclass(frozen=True)
class DirectoryScanPlan:
    command: list[str]
    mode: str
    state_key: str
    anchor_at: str
    paths: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "paths": self.paths, "anchor_at": self.anchor_at}


def state_key_for_job(job: JobDefinition) -> str:
    # Changing roots/options must not reuse another selection's reconciliation.
    payload = {"source": job.source_path, "destination": job.destination_path,
               "settings": job.directory_scan.to_dict(), "options": job.options.to_dict()}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    return f"directory_scan:{job.key}:{digest}"


def _parse_anchor(raw: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw or "")
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (TypeError, ValueError):
        return None


def directory_paths(job: JobDefinition, now: datetime) -> list[str]:
    settings = job.directory_scan.normalized()
    zone = ZoneInfo(settings.timezone)
    # Subtract elapsed days in UTC, THEN convert to calendar dates: handles DST.
    end = now.astimezone(zone).date()
    start = (now.astimezone(timezone.utc) - timedelta(days=settings.lookback_days)).astimezone(zone).date()
    paths: list[str] = []
    day = start
    while day <= end:
        path = day.strftime(settings.path_template)
        if path not in paths:
            paths.append(path)
        day += timedelta(days=1)
    return paths


def _strip_flags(command: list[str], flags: set[str]) -> list[str]:
    output: list[str] = []
    i = 0
    while i < len(command):
        value = command[i]
        if value in flags:
            i += 2
        elif value.split("=", 1)[0] in flags:
            i += 1
        else:
            output.append(value)
            i += 1
    return output


def build_scan_plan(job: JobDefinition, command: list[str], *, now: datetime,
                    anchor_at: str | None) -> DirectoryScanPlan:
    if now.tzinfo is None:
        raise ValueError("directory scan requires an aware timestamp")
    job = job.validate()
    settings = job.directory_scan
    if not settings.enabled:
        raise ValueError("directory scan is disabled")
    if len(command) < 4 or command[:2] != ["rclone", "copy"]:
        raise ValueError("directory scan supports rclone copy only")
    if command[2:4] != [job.source_path, job.destination_path]:
        raise ValueError("queued source/destination differs from directory scan settings; enqueue the job again")
    # A queued command may predate enabling this feature. Validate its actual
    # selectors too: a stale '+ /**' rule can defeat all later date/exclude rules.
    unsafe_selectors = {
        "--delete-excluded", "--filter", "--filter-from", "--include", "--include-from",
        "--exclude-from", "--exclude-if-present", "--files-from", "--files-from-raw",
        "--max-depth", "--min-size", "--max-size", "--ignore-case",
    }
    for token in command[4:]:
        if token.split("=", 1)[0] in unsafe_selectors or token.startswith("-f"):
            raise ValueError("queued command contains conflicting directory selectors; enqueue the job again")
    key = state_key_for_job(job)
    utc_now = now.astimezone(timezone.utc)
    anchor = _parse_anchor(anchor_at)
    # First optimized run establishes the clock, avoiding a surprise full walk.
    if anchor is None or anchor > utc_now:
        anchor = utc_now
    due = settings.full_scan_enabled and utc_now >= anchor + timedelta(hours=settings.full_scan_interval_hours)
    if due:
        full_command = list(command)
        if settings.full_scan_ignore_age:
            full_command = _strip_flags(full_command, {"--max-age", "--min-age"})
        return DirectoryScanPlan(full_command, "full", key, anchor.isoformat(), [])

    # Preserve the existing exclude semantics and place them BEFORE date includes.
    # Mixing --include with --exclude would let broad includes override exclusions.
    output: list[str] = []
    exclusions: list[str] = []
    i = 0
    while i < len(command):
        value = command[i]
        if value == "--exclude":
            if i + 1 >= len(command):
                raise ValueError("--exclude requires a pattern")
            exclusions.append(command[i + 1]); i += 2
        elif value.startswith("--exclude="):
            exclusions.append(value.split("=", 1)[1]); i += 1
        elif value == "--fast-list" or value.startswith("--fast-list="):
            # Recursive flat listing defeats directory pruning on ListR remotes.
            i += 1
        else:
            output.append(value); i += 1
    for pattern in exclusions:
        output.extend(["--filter", f"- {pattern}"])
    paths = directory_paths(job, utc_now)
    for path in paths:
        output.extend(["--filter", f"+ /{path}/**"])
    output.extend(["--filter", "- /**"])
    return DirectoryScanPlan(output, "window", key, anchor.isoformat(), paths)
