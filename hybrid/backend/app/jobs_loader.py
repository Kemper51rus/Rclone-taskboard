from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any

from .domain import (
    BackupOptions,
    BandwidthSettings,
    CloudSettings,
    DEFAULT_RCLONE_ARGS,
    DEFAULT_QUEUE_DEFINITIONS,
    GotifySettings,
    JobCatalog,
    JobDefinition,
    JobNotificationSettings,
    LoggingSettings,
    QueueDefinition,
    QueueSettings,
    RetentionSettings,
    ScheduleDefinition,
    WatcherSettings,
)


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    return text


def load_catalog(
    path: Path,
    *,
    standard_interval_minutes: int = 1,
    heavy_hour: int = 3,
    watcher_debounce_seconds: int = 45,
) -> JobCatalog:
    if not path.exists():
        _bootstrap_catalog_file(path)

    data = json.loads(path.read_text(encoding="utf-8"))
    raw_jobs = data.get("jobs", [])
    raw_profiles = data.get("profiles", {})
    raw_gotify = data.get("gotify", {})
    raw_queues = data.get("queues", {})
    raw_bandwidth = data.get("bandwidth", {})
    raw_logging = data.get("logging", {})
    raw_watcher = data.get("watcher", {})
    raw_clouds = data.get("clouds", [])

    if not isinstance(raw_jobs, list):
        raise ValueError("jobs must be a list")
    if not isinstance(raw_profiles, dict):
        raise ValueError("profiles must be an object")

    seen_keys: set[str] = set()
    jobs: list[JobDefinition] = []
    inferred_profiles = _normalize_profiles(raw_profiles)

    for index, raw in enumerate(raw_jobs, start=1):
        if not isinstance(raw, dict):
            raise ValueError("job item must be an object")
        job = _load_job(
            raw=raw,
            index=index,
            inferred_profiles=inferred_profiles,
            standard_interval_minutes=standard_interval_minutes,
            heavy_hour=heavy_hour,
        )
        if not job.key:
            raise ValueError("job key is required")
        if job.key in seen_keys:
            raise ValueError(f"duplicate job key: {job.key}")
        seen_keys.add(job.key)
        jobs.append(job)

    jobs, migrated = _migrate_retention_commands(jobs)
    gotify = _load_gotify_settings(raw_gotify)
    queues = _load_queue_settings(raw_queues)
    profiles = build_profiles(jobs, queue_keys=queues.queue_keys())
    bandwidth = _load_bandwidth_settings(raw_bandwidth)
    logging = _load_logging_settings(raw_logging)
    watcher = _load_watcher_settings(raw_watcher, default_debounce_seconds=watcher_debounce_seconds)
    clouds = _load_clouds(raw_clouds)
    catalog = JobCatalog(
        jobs=jobs,
        profiles=profiles,
        gotify=gotify,
        queues=queues,
        bandwidth=bandwidth,
        logging=logging,
        watcher=watcher,
        clouds=clouds,
    )
    if migrated:
        save_catalog(path, catalog)
    return catalog


def _bootstrap_catalog_file(path: Path) -> None:
    candidates = [
        path.with_name(path.name.replace(".json", ".example.json")),
        path.with_name(path.name.replace(".json", ".template.json")),
    ]
    for candidate in candidates:
        if candidate.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate, path)
            return
    raise FileNotFoundError(f"jobs file not found: {path}")


def save_catalog(path: Path, catalog: JobCatalog) -> None:
    jobs = [job_to_storage_dict(job) for job in catalog.raw_jobs()]
    payload = {
        "profiles": build_profiles(catalog.raw_jobs(), queue_keys=catalog.queues.queue_keys()),
        "gotify": catalog.gotify.to_dict(),
        "queues": catalog.queues.to_dict(),
        "bandwidth": catalog.bandwidth.to_dict(),
        "logging": catalog.logging.to_dict(),
        "watcher": catalog.watcher.to_dict(),
        # Cloud settings are sourced from rclone.conf at runtime and must not be persisted
        # back into the jobs catalog, otherwise credentials can leak into JSON.
        "clouds": [],
        "jobs": jobs,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_profiles(jobs: list[JobDefinition], queue_keys: list[str] | None = None) -> dict[str, list[str]]:
    profiles = {key: [] for key in (queue_keys or [])}
    profiles["all"] = []
    for job in sorted(jobs, key=lambda item: (item.order, item.key)):
        profiles["all"].append(job.key)
        profiles.setdefault(job.profile, []).append(job.key)
    return {name: keys for name, keys in profiles.items() if keys}


def job_to_storage_dict(job: JobDefinition) -> dict[str, Any]:
    normalized = job.validate()
    item: dict[str, Any] = {
        "key": normalized.key,
        "order": normalized.order,
        "description": normalized.description,
        "title": normalized.title,
        "timeout_seconds": normalized.timeout_seconds,
        "enabled": normalized.enabled,
        "continue_on_error": normalized.continue_on_error,
        "kind": normalized.kind,
        "profile": normalized.profile,
        "schedule": normalized.schedule.to_dict(),
        "notifications": normalized.notifications.to_dict(),
        "watcher_enabled": normalized.watcher_enabled,
    }
    if normalized.kind == "backup":
        item["source_path"] = normalized.source_path
        item["cloud_key"] = normalized.cloud_key
        item["destination_subpath"] = normalized.destination_subpath
        item["destination_path"] = normalized.destination_path
        item["transfer_mode"] = normalized.transfer_mode
        item["options"] = normalized.options.to_dict()
        item["retention"] = normalized.retention.to_dict()
    else:
        item["command"] = normalized.command
    return item


def cloud_to_storage_dict(cloud: CloudSettings) -> dict[str, Any]:
    normalized = cloud.normalized()
    return {
        "key": normalized.key,
        "title": normalized.title,
        "provider": normalized.provider,
        "remote_name": normalized.remote_name,
        "username": normalized.username,
        "token": normalized.token,
        "endpoint": normalized.endpoint,
        "root_path": normalized.root_path,
        "notes": normalized.notes,
        "extra_config": normalized.extra_config,
        "enabled": normalized.enabled,
    }


def _normalize_profiles(raw_profiles: dict[str, Any]) -> dict[str, list[str]]:
    profiles: dict[str, list[str]] = {}
    for profile_name, profile_steps in raw_profiles.items():
        if not isinstance(profile_name, str):
            raise ValueError("profile name must be a string")
        if not isinstance(profile_steps, list) or not all(
            isinstance(item, str) for item in profile_steps
        ):
            raise ValueError(f"profile '{profile_name}' must be a list of job keys")
        profiles[profile_name] = profile_steps
    return profiles


def _load_job(
    *,
    raw: dict[str, Any],
    index: int,
    inferred_profiles: dict[str, list[str]],
    standard_interval_minutes: int,
    heavy_hour: int,
) -> JobDefinition:
    key = str(raw.get("key", "")).strip()
    kind = str(raw.get("kind", "")).strip() or None
    profile = str(raw.get("profile", "")).strip() or _infer_profile(key, inferred_profiles)
    order = int(raw.get("order", index))
    description = str(raw.get("description", key)).strip() or key
    title = str(raw.get("title", "")).strip() or None
    timeout_seconds = max(1, int(raw.get("timeout_seconds", 3600)))
    enabled = bool(raw.get("enabled", True))
    continue_on_error = bool(raw.get("continue_on_error", False))

    if kind == "backup" or (kind is None and _looks_like_backup(raw.get("command"))):
        source_path, destination_path, transfer_mode, options = _extract_backup_fields(raw)
        return JobDefinition(
            key=key,
            order=order,
            description=description,
            title=title,
            timeout_seconds=timeout_seconds,
            enabled=enabled,
            continue_on_error=continue_on_error,
            kind="backup",
            profile=profile,
            schedule=_load_schedule(raw.get("schedule"), profile, standard_interval_minutes, heavy_hour),
            source_path=source_path,
            cloud_key=_clean_optional_text(raw.get("cloud_key")),
            destination_subpath=_clean_optional_text(raw.get("destination_subpath")),
            destination_path=destination_path,
            transfer_mode=transfer_mode,
            options=options,
            retention=_load_retention(raw.get("retention")),
            notifications=_load_notifications(raw.get("notifications")),
            watcher_enabled=bool(raw.get("watcher_enabled", False)),
        ).validate()

    command = raw.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError(f"job '{key}' command must be a list of strings")

    return JobDefinition(
        key=key,
        order=order,
        description=description,
        title=title,
        timeout_seconds=timeout_seconds,
        enabled=enabled,
        continue_on_error=continue_on_error,
        kind="command",
        profile=profile,
        schedule=_load_schedule(raw.get("schedule"), profile, standard_interval_minutes, heavy_hour),
        command=command,
        notifications=_load_notifications(raw.get("notifications")),
    ).validate()


def _load_schedule(
    raw_schedule: Any,
    profile: str,
    standard_interval_minutes: int,
    heavy_hour: int,
) -> ScheduleDefinition:
    if isinstance(raw_schedule, dict):
        return ScheduleDefinition(
            enabled=bool(raw_schedule.get("enabled", False)),
            mode=str(raw_schedule.get("mode", "manual")),
            interval_minutes=int(raw_schedule.get("interval_minutes", 60)),
            hour=int(raw_schedule.get("hour", 3)),
            minute=int(raw_schedule.get("minute", 0)),
            weekdays=[int(day) for day in raw_schedule.get("weekdays", []) if str(day).strip()],
        ).validate()

    if profile == "heavy":
        return ScheduleDefinition(enabled=True, mode="daily", hour=heavy_hour, minute=0).validate()
    return ScheduleDefinition(
        enabled=True,
        mode="interval",
        interval_minutes=max(1, standard_interval_minutes),
    ).validate()


def _infer_profile(key: str, profiles: dict[str, list[str]]) -> str:
    if key in profiles.get("heavy", []):
        return "heavy"
    if key in profiles.get("standard", []):
        return "standard"
    for profile_name, job_keys in profiles.items():
        if profile_name == "all":
            continue
        if key in job_keys:
            return profile_name
    return "standard"


def _load_gotify_settings(raw: Any) -> GotifySettings:
    if not isinstance(raw, dict):
        return GotifySettings()
    return GotifySettings(
        enabled=bool(raw.get("enabled", False)),
        url=raw.get("url"),
        token=raw.get("token"),
        default_priority=int(raw.get("default_priority", 5)),
    ).normalized()


def _load_notifications(raw: Any) -> JobNotificationSettings:
    if not isinstance(raw, dict):
        return JobNotificationSettings()
    priority = raw.get("priority")
    return JobNotificationSettings(
        on_success=bool(raw.get("on_success", False)),
        on_failure=bool(raw.get("on_failure", True)),
        priority=int(priority) if priority not in (None, "") else None,
        custom_title=raw.get("custom_title"),
    ).normalized()


def _load_queue_settings(raw: Any) -> QueueSettings:
    if not isinstance(raw, dict):
        return QueueSettings(definitions=[definition.normalized() for definition in DEFAULT_QUEUE_DEFINITIONS])
    raw_definitions = raw.get("definitions", [])
    definitions: list[QueueDefinition] = []
    if isinstance(raw_definitions, list):
        for item in raw_definitions:
            if not isinstance(item, dict):
                continue
            definitions.append(
                QueueDefinition(
                    key=str(item.get("key", "")).strip(),
                    title=item.get("title"),
                    workers=int(item.get("workers", 1) or 1),
                    bandwidth_limit=item.get("bandwidth_limit"),
                    enabled=bool(item.get("enabled", True)),
                ).normalized()
            )
    return QueueSettings(
        allow_parallel_profiles=bool(raw.get("allow_parallel_profiles", False)),
        allow_scheduler_queueing=bool(raw.get("allow_scheduler_queueing", False)),
        allow_event_queueing=bool(raw.get("allow_event_queueing", False)),
        definitions=definitions,
    ).normalized()


def _load_bandwidth_settings(raw: Any) -> BandwidthSettings:
    if isinstance(raw, str):
        return BandwidthSettings(limit=raw).normalized()
    if not isinstance(raw, dict):
        return BandwidthSettings()
    return BandwidthSettings(
        limit=raw.get("limit"),
    ).normalized()


def _load_logging_settings(raw: Any) -> LoggingSettings:
    if isinstance(raw, bool):
        return LoggingSettings(rclone_log_enabled=raw).normalized()
    if not isinstance(raw, dict):
        return LoggingSettings()
    return LoggingSettings(
        rclone_log_enabled=bool(raw.get("rclone_log_enabled", False)),
        auto_rclone_log_enabled=bool(raw.get("auto_rclone_log_enabled", False)),
        auto_rclone_log_threshold=int(raw.get("auto_rclone_log_threshold", 3) or 3),
    ).normalized()


def _load_watcher_settings(raw: Any, *, default_debounce_seconds: int) -> WatcherSettings:
    if isinstance(raw, bool):
        return WatcherSettings(
            enabled=raw,
            debounce_seconds=default_debounce_seconds,
        ).normalized()
    if not isinstance(raw, dict):
        return WatcherSettings(debounce_seconds=default_debounce_seconds).normalized()
    return WatcherSettings(
        enabled=bool(raw.get("enabled", False)),
        debounce_seconds=int(raw.get("debounce_seconds", default_debounce_seconds) or default_debounce_seconds),
    ).normalized()


def _load_retention(raw: Any) -> RetentionSettings:
    if not isinstance(raw, dict):
        return RetentionSettings()
    return RetentionSettings(
        enabled=bool(raw.get("enabled", False)),
        min_age=raw.get("min_age"),
        exclude=list(raw.get("exclude", [])),
        extra_args=list(raw.get("extra_args", [])),
    ).normalized()


def _load_clouds(raw: Any) -> list[CloudSettings]:
    if not isinstance(raw, list):
        return []
    clouds: list[CloudSettings] = []
    seen_keys: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        cloud = CloudSettings(
            key=str(item.get("key", "")).strip(),
            title=str(item.get("title", "")).strip(),
            provider=str(item.get("provider", "generic")).strip() or "generic",
            remote_name=_clean_optional_text(item.get("remote_name")),
            username=_clean_optional_text(item.get("username")),
            token=_clean_optional_text(item.get("token")),
            endpoint=_clean_optional_text(item.get("endpoint")),
            root_path=_clean_optional_text(item.get("root_path")),
            notes=_clean_optional_text(item.get("notes")),
            extra_config={
                str(config_key).strip(): str(config_value).strip()
                for config_key, config_value in (item.get("extra_config", {}) or {}).items()
                if str(config_key).strip() and str(config_value).strip()
            },
            enabled=bool(item.get("enabled", True)),
        ).normalized()
        if not cloud.key or cloud.key in seen_keys:
            continue
        seen_keys.add(cloud.key)
        clouds.append(cloud)
    return clouds


def _looks_like_backup(command: Any) -> bool:
    if not isinstance(command, list) or len(command) < 4:
        return False
    if not all(isinstance(item, str) for item in command):
        return False
    if command[0] != "rclone" or command[1] not in {"copy", "sync"}:
        return False
    source = command[2].strip()
    destination = command[3].strip()
    return source.startswith("/") and ":" in destination


def _extract_backup_fields(raw: dict[str, Any]) -> tuple[str, str, str, BackupOptions]:
    command = raw.get("command")
    if isinstance(command, list) and _looks_like_backup(command):
        source_path = str(raw.get("source_path", command[2])).strip()
        destination_path = str(raw.get("destination_path", command[3])).strip()
        transfer_mode = str(raw.get("transfer_mode", command[1])).strip() or "copy"
        options = _extract_options_from_command(command[4:])
    else:
        source_path = str(raw.get("source_path", "")).strip()
        destination_path = str(raw.get("destination_path", "")).strip()
        transfer_mode = str(raw.get("transfer_mode", "copy")).strip() or "copy"
        options = BackupOptions()

    raw_options = raw.get("options")
    if isinstance(raw_options, dict):
        options = BackupOptions(
            max_age=raw_options.get("max_age"),
            min_age=raw_options.get("min_age"),
            exclude=list(raw_options.get("exclude", [])),
            extra_args=list(raw_options.get("extra_args", [])),
        )
    return source_path, destination_path, transfer_mode, options.normalized()


def _extract_options_from_command(args: list[str]) -> BackupOptions:
    max_age: str | None = None
    min_age: str | None = None
    exclude: list[str] = []
    extra_args: list[str] = []

    i = 0
    while i < len(args):
        current = args[i]
        next_value = args[i + 1] if i + 1 < len(args) else None
        if current == "--max-age" and next_value is not None:
            max_age = next_value
            i += 2
            continue
        if current == "--min-age" and next_value is not None:
            min_age = next_value
            i += 2
            continue
        if current == "--exclude" and next_value is not None:
            exclude.append(next_value)
            i += 2
            continue
        if _matches_default_args(args, i):
            i += len(_DEFAULT_ARGS_SLICE)
            continue
        extra_args.append(current)
        if next_value is not None and not next_value.startswith("--"):
            extra_args.append(next_value)
            i += 2
            continue
        i += 1

    return BackupOptions(max_age=max_age, min_age=min_age, exclude=exclude, extra_args=extra_args)


_DEFAULT_ARGS_SLICE = [
    "--contimeout",
    "30s",
    "--timeout",
    "10m",
    "--retries",
    "8",
    "--retries-sleep",
    "20s",
    "--low-level-retries",
    "20",
    "--transfers",
    "2",
    "--checkers",
    "8",
    "--stats",
    "10s",
    "--stats-one-line",
    "--log-file",
    "/var/log/rclone-backup.log",
    "--log-level",
    "INFO",
]


def _matches_default_args(args: list[str], start: int) -> bool:
    end = start + len(_DEFAULT_ARGS_SLICE)
    return args[start:end] == _DEFAULT_ARGS_SLICE


def _migrate_retention_commands(jobs: list[JobDefinition]) -> tuple[list[JobDefinition], bool]:
    migrated = False
    migrated_jobs = list(jobs)
    backup_index_by_key: dict[tuple[str, str, tuple[Any, ...]], int] = {}

    for index, job in enumerate(migrated_jobs):
        if job.kind != "backup" or not job.destination_path:
            continue
        backup_index_by_key[_retention_match_key(job)] = index

    retained_jobs: list[JobDefinition] = []
    for job in migrated_jobs:
        retention_info = _extract_retention_from_command_job(job)
        if not retention_info:
            retained_jobs.append(job)
            continue

        destination_path, retention_settings = retention_info
        backup_lookup_key = (destination_path, job.profile, _schedule_match_key(job.schedule))
        backup_index = backup_index_by_key.get(backup_lookup_key)
        if backup_index is None:
            retained_jobs.append(job)
            continue

        backup = migrated_jobs[backup_index]
        if backup.retention.enabled:
            retained_jobs.append(job)
            continue

        migrated_backup = JobDefinition(
            key=backup.key,
            order=backup.order,
            description=backup.description,
            timeout_seconds=backup.timeout_seconds,
            enabled=backup.enabled,
            continue_on_error=backup.continue_on_error,
            title=backup.title,
            kind=backup.kind,
            profile=backup.profile,
            schedule=backup.schedule,
            command=backup.command,
            source_path=backup.source_path,
            cloud_key=backup.cloud_key,
            destination_subpath=backup.destination_subpath,
            destination_path=backup.destination_path,
            transfer_mode=backup.transfer_mode,
            options=backup.options,
            retention=RetentionSettings(
                enabled=job.enabled,
                min_age=retention_settings.min_age,
                exclude=retention_settings.exclude,
                extra_args=retention_settings.extra_args,
            ),
            notifications=backup.notifications,
        ).validate()
        migrated_jobs[backup_index] = migrated_backup
        backup_index_by_key[_retention_match_key(migrated_backup)] = backup_index
        retained_jobs = [
            migrated_backup if existing.key == backup.key else existing
            for existing in retained_jobs
        ]
        migrated = True

    retained_jobs.sort(key=lambda item: (item.order, item.key))
    return retained_jobs, migrated


def _retention_match_key(job: JobDefinition) -> tuple[str, str, tuple[Any, ...]]:
    return (
        str(job.destination_path or "").strip(),
        job.profile,
        _schedule_match_key(job.schedule),
    )


def _schedule_match_key(schedule: ScheduleDefinition) -> tuple[Any, ...]:
    normalized = schedule.validate()
    return (
        normalized.enabled,
        normalized.mode,
        normalized.interval_minutes,
        normalized.hour,
        normalized.minute,
        tuple(normalized.weekdays),
    )


def _extract_retention_from_command_job(job: JobDefinition) -> tuple[str, RetentionSettings] | None:
    if job.kind != "command":
        return None
    command = list(job.command)
    if len(command) < 4 or command[0] != "rclone" or command[1] != "delete":
        return None

    destination_path = str(command[2]).strip()
    args = command[3:]
    if len(args) >= len(DEFAULT_RCLONE_ARGS) and args[-len(DEFAULT_RCLONE_ARGS):] == DEFAULT_RCLONE_ARGS:
        args = args[:-len(DEFAULT_RCLONE_ARGS)]

    min_age: str | None = None
    exclude: list[str] = []
    extra_args: list[str] = []
    i = 0
    while i < len(args):
        current = args[i]
        next_value = args[i + 1] if i + 1 < len(args) else None
        if current == "--min-age" and next_value is not None:
            min_age = next_value
            i += 2
            continue
        if current == "--exclude" and next_value is not None:
            exclude.append(next_value)
            i += 2
            continue
        extra_args.append(current)
        if next_value is not None and not next_value.startswith("--"):
            extra_args.append(next_value)
            i += 2
            continue
        i += 1

    if not destination_path or not min_age:
        return None
    return (
        destination_path,
        RetentionSettings(
            enabled=job.enabled,
            min_age=min_age,
            exclude=exclude,
            extra_args=extra_args,
        ).normalized(),
    )
