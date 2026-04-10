from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import threading
from typing import Any


VALID_TRANSFER_MODES = {"copy", "sync"}
VALID_JOB_KINDS = {"backup", "command"}
VALID_SCHEDULE_MODES = {"manual", "interval", "daily", "weekly"}
DISABLED_BWLIMIT_VALUES = {"off", "none", "unlimited", "disabled", "0", "0b", "0k", "0m", "0g"}

DEFAULT_RCLONE_ARGS = [
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
    "--log-level",
    "INFO",
]


@dataclass(frozen=True)
class ScheduleDefinition:
    enabled: bool = False
    mode: str = "manual"
    interval_minutes: int = 60
    hour: int = 3
    minute: int = 0
    weekdays: list[int] = field(default_factory=list)

    def validate(self) -> ScheduleDefinition:
        mode = self.mode if self.mode in VALID_SCHEDULE_MODES else "manual"
        interval = max(1, int(self.interval_minutes or 1))
        hour = min(23, max(0, int(self.hour or 0)))
        minute = min(59, max(0, int(self.minute or 0)))
        weekdays = sorted({day for day in self.weekdays if isinstance(day, int) and 0 <= day <= 6})
        enabled = bool(self.enabled) and mode != "manual"
        return ScheduleDefinition(
            enabled=enabled,
            mode=mode,
            interval_minutes=interval,
            hour=hour,
            minute=minute,
            weekdays=weekdays,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.validate())

    def summary(self) -> str:
        schedule = self.validate()
        if not schedule.enabled:
            return "manual"
        if schedule.mode == "interval":
            return f"every {schedule.interval_minutes} min"
        if schedule.mode == "daily":
            return f"daily {schedule.hour:02d}:{schedule.minute:02d}"
        if schedule.mode == "weekly":
            labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            days = ", ".join(labels[day] for day in schedule.weekdays) or "custom"
            return f"{days} {schedule.hour:02d}:{schedule.minute:02d}"
        return "manual"

    def due_slot(self, now_local: datetime) -> str | None:
        schedule = self.validate()
        if not schedule.enabled:
            return None
        if schedule.mode == "interval":
            total_minutes = now_local.hour * 60 + now_local.minute
            if total_minutes % schedule.interval_minutes != 0:
                return None
            return now_local.strftime("%Y-%m-%dT%H:%M")
        if schedule.mode == "daily":
            if now_local.hour != schedule.hour or now_local.minute != schedule.minute:
                return None
            return now_local.strftime("%Y-%m-%d")
        if schedule.mode == "weekly":
            if now_local.weekday() not in schedule.weekdays:
                return None
            if now_local.hour != schedule.hour or now_local.minute != schedule.minute:
                return None
            return now_local.strftime("%G-W%V-%u")
        return None


@dataclass(frozen=True)
class BackupOptions:
    max_age: str | None = None
    min_age: str | None = None
    exclude: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)

    def normalized(self) -> BackupOptions:
        return BackupOptions(
            max_age=(self.max_age or "").strip() or None,
            min_age=(self.min_age or "").strip() or None,
            exclude=[str(item).strip() for item in self.exclude if str(item).strip()],
            extra_args=[str(item).strip() for item in self.extra_args if str(item).strip()],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())

    def to_args(self) -> list[str]:
        options = self.normalized()
        args: list[str] = []
        if options.max_age:
            args.extend(["--max-age", options.max_age])
        if options.min_age:
            args.extend(["--min-age", options.min_age])
        for pattern in options.exclude:
            args.extend(["--exclude", pattern])
        args.extend(options.extra_args)
        return args


@dataclass(frozen=True)
class RetentionSettings:
    enabled: bool = False
    min_age: str | None = None
    exclude: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)

    def normalized(self) -> RetentionSettings:
        return RetentionSettings(
            enabled=bool(self.enabled) and bool((self.min_age or "").strip()),
            min_age=(self.min_age or "").strip() or None,
            exclude=[str(item).strip() for item in self.exclude if str(item).strip()],
            extra_args=[str(item).strip() for item in self.extra_args if str(item).strip()],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())

    def to_args(self) -> list[str]:
        retention = self.normalized()
        args: list[str] = []
        if retention.min_age:
            args.extend(["--min-age", retention.min_age])
        for pattern in retention.exclude:
            args.extend(["--exclude", pattern])
        args.extend(retention.extra_args)
        return args


@dataclass(frozen=True)
class GotifySettings:
    enabled: bool = False
    url: str | None = None
    token: str | None = None
    default_priority: int = 5

    def normalized(self) -> GotifySettings:
        priority = int(self.default_priority or 5)
        return GotifySettings(
            enabled=bool(self.enabled),
            url=(self.url or "").strip() or None,
            token=(self.token or "").strip() or None,
            default_priority=min(10, max(1, priority)),
        )

    def is_configured(self) -> bool:
        normalized = self.normalized()
        return bool(normalized.enabled and normalized.url and normalized.token)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())


@dataclass(frozen=True)
class QueueDefinition:
    key: str
    title: str | None = None
    workers: int = 1
    bandwidth_limit: str | None = None
    enabled: bool = True

    def normalized(self) -> QueueDefinition:
        key = str(self.key or "").strip() or "standard"
        title = str(self.title or "").strip() or key
        workers = max(1, int(self.workers or 1))
        return QueueDefinition(
            key=key,
            title=title,
            workers=workers,
            bandwidth_limit=normalize_bwlimit(self.bandwidth_limit),
            enabled=bool(self.enabled),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())


DEFAULT_QUEUE_DEFINITIONS = [
    QueueDefinition(key="standard", title="standard", workers=1, enabled=True),
    QueueDefinition(key="heavy", title="heavy", workers=1, enabled=True),
]


@dataclass(frozen=True)
class QueueSettings:
    allow_parallel_profiles: bool = False
    allow_scheduler_queueing: bool = False
    allow_event_queueing: bool = False
    definitions: list[QueueDefinition] = field(default_factory=list)

    def normalized(self) -> QueueSettings:
        normalized_definitions: list[QueueDefinition] = []
        seen_keys: set[str] = set()
        for definition in self.definitions or []:
            normalized = definition.normalized()
            if not normalized.key or normalized.key in seen_keys:
                continue
            seen_keys.add(normalized.key)
            normalized_definitions.append(normalized)
        if not normalized_definitions:
            normalized_definitions = [definition.normalized() for definition in DEFAULT_QUEUE_DEFINITIONS]
        return QueueSettings(
            allow_parallel_profiles=bool(self.allow_parallel_profiles),
            allow_scheduler_queueing=bool(self.allow_scheduler_queueing),
            allow_event_queueing=bool(self.allow_event_queueing),
            definitions=normalized_definitions,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())

    def queue_keys(self) -> list[str]:
        return [definition.key for definition in self.normalized().definitions if definition.enabled]


@dataclass(frozen=True)
class BandwidthSettings:
    limit: str | None = None

    def normalized(self) -> BandwidthSettings:
        raw_limit = str(self.limit or "").strip()
        normalized_limit = raw_limit or None
        if normalized_limit and normalized_limit.lower() in DISABLED_BWLIMIT_VALUES:
            normalized_limit = None
        return BandwidthSettings(limit=normalized_limit)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())


@dataclass(frozen=True)
class LoggingSettings:
    rclone_log_enabled: bool = False

    def normalized(self) -> LoggingSettings:
        return LoggingSettings(
            rclone_log_enabled=bool(self.rclone_log_enabled),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())


@dataclass(frozen=True)
class CloudSettings:
    key: str
    title: str
    provider: str = "generic"
    remote_name: str | None = None
    username: str | None = None
    token: str | None = None
    endpoint: str | None = None
    root_path: str | None = None
    notes: str | None = None
    extra_config: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def normalized(self) -> CloudSettings:
        key = str(self.key or "").strip()
        title = str(self.title or "").strip() or key
        provider = str(self.provider or "generic").strip().lower() or "generic"
        extra_config = {
            str(config_key).strip(): str(config_value).strip()
            for config_key, config_value in (self.extra_config or {}).items()
            if str(config_key).strip() and str(config_value).strip()
        }
        return CloudSettings(
            key=key,
            title=title,
            provider=provider,
            remote_name=(self.remote_name or "").strip() or None,
            username=(self.username or "").strip() or None,
            token=(self.token or "").strip() or None,
            endpoint=(self.endpoint or "").strip() or None,
            root_path=(self.root_path or "").strip() or None,
            notes=(self.notes or "").strip() or None,
            extra_config=extra_config,
            enabled=bool(self.enabled),
        )

    def to_dict(self) -> dict[str, Any]:
        normalized = self.normalized()
        item = asdict(normalized)
        item["display_title"] = normalized.title or normalized.key
        return item


@dataclass(frozen=True)
class JobNotificationSettings:
    on_success: bool = False
    on_failure: bool = True
    priority: int | None = None
    custom_title: str | None = None

    def normalized(self) -> JobNotificationSettings:
        priority = self.priority
        if priority is not None:
            priority = min(10, max(1, int(priority)))
        return JobNotificationSettings(
            on_success=bool(self.on_success),
            on_failure=bool(self.on_failure),
            priority=priority,
            custom_title=(self.custom_title or "").strip() or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())


@dataclass(frozen=True)
class JobDefinition:
    key: str
    order: int
    description: str
    timeout_seconds: int
    enabled: bool
    continue_on_error: bool
    title: str | None = None
    kind: str = "command"
    profile: str = "standard"
    schedule: ScheduleDefinition = field(default_factory=ScheduleDefinition)
    command: list[str] = field(default_factory=list)
    source_path: str | None = None
    cloud_key: str | None = None
    destination_subpath: str | None = None
    destination_path: str | None = None
    transfer_mode: str = "copy"
    options: BackupOptions = field(default_factory=BackupOptions)
    retention: RetentionSettings = field(default_factory=RetentionSettings)
    notifications: JobNotificationSettings = field(default_factory=JobNotificationSettings)

    def validate(self) -> JobDefinition:
        raw_key = self.key.strip()
        kind = self.kind if self.kind in VALID_JOB_KINDS else "command"
        profile = (self.profile or "").strip() or "standard"
        transfer_mode = self.transfer_mode if self.transfer_mode in VALID_TRANSFER_MODES else "copy"
        source_path = (self.source_path or "").strip() or None
        cloud_key = (self.cloud_key or "").strip() or None
        destination_subpath = (self.destination_subpath or "").strip() or None
        destination_path = (self.destination_path or "").strip() or None
        command = [item for item in self.command if isinstance(item, str) and item.strip()]
        schedule = self.schedule.validate()
        options = self.options.normalized()
        retention = self.retention.normalized()
        notifications = self.notifications.normalized()
        description = self.description.strip() or raw_key
        title = (self.title or "").strip() or description
        if kind != "backup" or transfer_mode == "sync":
            retention = RetentionSettings()
        if kind == "backup" and source_path and destination_path:
            command = self.build_backup_command(
                transfer_mode=transfer_mode,
                source_path=source_path,
                destination_path=destination_path,
                options=options,
            )
        return JobDefinition(
            key=raw_key,
            order=max(1, int(self.order or 1)),
            description=description,
            title=title,
            timeout_seconds=max(1, int(self.timeout_seconds or 1)),
            enabled=bool(self.enabled),
            continue_on_error=True,
            kind=kind,
            profile=profile,
            schedule=schedule,
            command=command,
            source_path=source_path,
            cloud_key=cloud_key,
            destination_subpath=destination_subpath,
            destination_path=destination_path,
            transfer_mode=transfer_mode,
            options=options,
            retention=retention,
            notifications=notifications,
        )

    def to_dict(self) -> dict[str, Any]:
        normalized = self.validate()
        item = asdict(normalized)
        item["display_title"] = normalized.title or normalized.description
        item["schedule_summary"] = normalized.schedule.summary()
        item["command_preview"] = " ".join(normalized.command)
        return item

    @staticmethod
    def build_backup_command(
        transfer_mode: str,
        source_path: str,
        destination_path: str,
        options: BackupOptions,
        bandwidth_limit: str | None = None,
    ) -> list[str]:
        command = [
            "rclone",
            transfer_mode,
            source_path,
            destination_path,
            *options.to_args(),
            *DEFAULT_RCLONE_ARGS,
        ]
        return apply_rclone_bwlimit(command, bandwidth_limit)

    @staticmethod
    def build_retention_command(
        destination_path: str,
        retention: RetentionSettings,
        bandwidth_limit: str | None = None,
    ) -> list[str]:
        normalized = retention.normalized()
        command = [
            "rclone",
            "delete",
            destination_path,
            *normalized.to_args(),
            *DEFAULT_RCLONE_ARGS,
        ]
        return apply_rclone_bwlimit(command, bandwidth_limit)


@dataclass(frozen=True)
class RunStepDefinition:
    job_key: str
    description: str
    command: list[str]
    timeout_seconds: int
    continue_on_error: bool
    step_kind: str = "job"


class JobCatalog:
    def __init__(
        self,
        jobs: list[JobDefinition],
        profiles: dict[str, list[str]],
        gotify: GotifySettings | None = None,
        queues: QueueSettings | None = None,
        bandwidth: BandwidthSettings | None = None,
        logging: LoggingSettings | None = None,
        clouds: list[CloudSettings] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._jobs_by_key: dict[str, JobDefinition] = {}
        self._clouds_by_key: dict[str, CloudSettings] = {}
        self._queue_definitions_by_key: dict[str, QueueDefinition] = {}
        self.profiles: dict[str, list[str]] = {}
        self.gotify = (gotify or GotifySettings()).normalized()
        self.queues = (queues or QueueSettings()).normalized()
        self.bandwidth = (bandwidth or BandwidthSettings()).normalized()
        self.logging = (logging or LoggingSettings()).normalized()
        self.replace(
            jobs=jobs,
            profiles=profiles,
            gotify=gotify,
            queues=queues,
            bandwidth=bandwidth,
            logging=logging,
            clouds=clouds,
        )

    def replace(
        self,
        jobs: list[JobDefinition],
        profiles: dict[str, list[str]],
        gotify: GotifySettings | None = None,
        queues: QueueSettings | None = None,
        bandwidth: BandwidthSettings | None = None,
        logging: LoggingSettings | None = None,
        clouds: list[CloudSettings] | None = None,
    ) -> None:
        normalized_jobs = {job.key: job.validate() for job in jobs}
        normalized_clouds = {
            cloud.key: cloud.normalized()
            for cloud in (clouds or self._clouds_by_key.values())
            if cloud.key
        }
        normalized_profiles = {name: list(keys) for name, keys in profiles.items()}
        normalized_queue_definitions = {
            definition.key: definition
            for definition in (queues or self.queues).normalized().definitions
            if definition.key
        }
        with self._lock:
            self._jobs_by_key = normalized_jobs
            self._clouds_by_key = normalized_clouds
            self._queue_definitions_by_key = normalized_queue_definitions
            self.profiles = normalized_profiles
            self.gotify = (gotify or self.gotify).normalized()
            self.queues = (queues or self.queues).normalized()
            self.bandwidth = (bandwidth or self.bandwidth).normalized()
            self.logging = (logging or self.logging).normalized()

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(self._jobs_by_key.values(), key=lambda job: (job.order, job.key))
            return [job.to_dict() for job in ordered]

    def raw_jobs(self) -> list[JobDefinition]:
        with self._lock:
            return sorted(self._jobs_by_key.values(), key=lambda job: (job.order, job.key))

    def list_clouds(self) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(self._clouds_by_key.values(), key=lambda cloud: (cloud.title, cloud.key))
            return [cloud.to_dict() for cloud in ordered]

    def raw_clouds(self) -> list[CloudSettings]:
        with self._lock:
            return sorted(self._clouds_by_key.values(), key=lambda cloud: (cloud.title, cloud.key))

    def get_cloud(self, key: str) -> CloudSettings | None:
        with self._lock:
            return self._clouds_by_key.get(key)

    def get_job(self, key: str) -> JobDefinition | None:
        with self._lock:
            return self._jobs_by_key.get(key)

    def list_queue_definitions(self) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(self._queue_definitions_by_key.values(), key=lambda item: item.key)
            return [item.to_dict() for item in ordered]

    def raw_queue_definitions(self) -> list[QueueDefinition]:
        with self._lock:
            return sorted(self._queue_definitions_by_key.values(), key=lambda item: item.key)

    def get_queue_definition(self, key: str) -> QueueDefinition | None:
        with self._lock:
            return self._queue_definitions_by_key.get(key)

    def list_backup_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [job for job in self._jobs_by_key.values() if job.kind == "backup"]
            jobs.sort(key=lambda job: (job.order, job.key))
            return [job.to_dict() for job in jobs]

    def list_command_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [job for job in self._jobs_by_key.values() if job.kind == "command"]
            jobs.sort(key=lambda job: (job.order, job.key))
            return [job.to_dict() for job in jobs]

    def get_profile_keys(self, profile: str) -> list[str]:
        with self._lock:
            if profile not in self.profiles:
                raise ValueError(f"unknown profile '{profile}'")
            return list(self.profiles[profile])

    def steps_for_profile(self, profile: str) -> list[JobDefinition]:
        return self.steps_for_keys(self.get_profile_keys(profile))

    def steps_for_keys(self, keys: list[str]) -> list[JobDefinition]:
        with self._lock:
            jobs: list[JobDefinition] = []
            for key in keys:
                job = self._jobs_by_key.get(key)
                if not job:
                    raise ValueError(f"unknown job '{key}'")
                if job.enabled:
                    jobs.append(job)
        jobs.sort(key=lambda item: (item.order, item.key))
        return jobs


def normalize_bwlimit(limit: str | None) -> str | None:
    raw_limit = str(limit or "").strip()
    if not raw_limit or raw_limit.lower() in DISABLED_BWLIMIT_VALUES:
        return None
    return raw_limit


def apply_rclone_bwlimit(command: list[str], limit: str | None) -> list[str]:
    normalized_limit = normalize_bwlimit(limit)
    if not command or command[0] != "rclone":
        return list(command)

    cleaned: list[str] = []
    skip_next = False
    for index, part in enumerate(command):
        if skip_next:
            skip_next = False
            continue
        if part == "--bwlimit":
            skip_next = index + 1 < len(command)
            continue
        if part.startswith("--bwlimit="):
            continue
        cleaned.append(part)

    if normalized_limit:
        cleaned.extend(["--bwlimit", normalized_limit])
    return cleaned


def effective_bwlimit(global_limit: str | None, queue_limit: str | None) -> str | None:
    return normalize_bwlimit(queue_limit) or normalize_bwlimit(global_limit)
