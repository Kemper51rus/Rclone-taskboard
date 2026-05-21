from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
import shlex
import threading
from typing import Any


VALID_TRANSFER_MODES = {"copy", "sync"}
VALID_JOB_KINDS = {"backup", "command"}
VALID_SCHEDULE_MODES = {"manual", "interval", "daily", "weekly"}
VALID_EXCLUDE_PATH_KINDS = {"directory", "file"}
ARCHIVE_FILENAME_TOKENS = {"{key}", "{date}"}
DISABLED_BWLIMIT_VALUES = {"off", "none", "unlimited", "disabled", "0", "0b", "0k", "0m", "0g"}
VALID_DEBUG_DUMP_VALUES = {"headers", "headers,bodies"}
SINGLETON_RCLONE_FLAGS = {
    "--transfers",
    "--checkers",
    "--tpslimit",
    "--tpslimit-burst",
    "--retries",
    "--low-level-retries",
    "--retries-sleep",
    "--timeout",
    "--contimeout",
    "--stats",
    "--stats-file-name-length",
    "--log-level",
    "--dump",
}

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
    "1s",
    "--stats-file-name-length",
    "96",
    "--log-level",
    "INFO",
]


def _normalize_optional_text(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_optional_int(value: Any, *, minimum: int = 0) -> int | None:
    if value in (None, ""):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, normalized)


def _normalize_optional_float(value: Any, *, minimum: float = 0.0) -> float | None:
    if value in (None, ""):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, normalized)


def _normalize_debug_dump(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_DEBUG_DUMP_VALUES else None


def _split_extra_args(extra_args: list[str]) -> list[str]:
    tokens: list[str] = []
    for raw in extra_args:
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = [str(raw).strip()]
        tokens.extend(part for part in parts if str(part).strip())
    return tokens


def _append_singleton_arg(args: list[str], flag: str, value: str | int | float | None) -> None:
    if value in (None, ""):
        return
    rendered = f"{value:g}" if isinstance(value, float) else str(value)
    args.extend([flag, rendered])


@dataclass(frozen=True)
class ExcludePathEntry:
    path: str
    kind: str = "directory"

    def normalized(self) -> ExcludePathEntry:
        normalized_path = str(self.path or "").strip()
        normalized_kind = str(self.kind or "directory").strip().lower()
        if normalized_kind not in VALID_EXCLUDE_PATH_KINDS:
            normalized_kind = "directory"
        if normalized_path.endswith("/") and normalized_path != "/":
            normalized_path = normalized_path.rstrip("/")
        return ExcludePathEntry(path=normalized_path, kind=normalized_kind)


def _normalize_exclude_path_entry(raw: Any) -> ExcludePathEntry | None:
    if isinstance(raw, ExcludePathEntry):
        normalized = raw.normalized()
    elif isinstance(raw, dict):
        normalized = ExcludePathEntry(
            path=str(raw.get("path", "")).strip(),
            kind=str(raw.get("kind", "directory")).strip().lower(),
        ).normalized()
    elif isinstance(raw, str):
        stripped = raw.strip()
        normalized = ExcludePathEntry(
            path=stripped.rstrip("/") if stripped.endswith("/") and stripped != "/" else stripped,
            kind="directory" if stripped.endswith("/") else "file",
        ).normalized()
    else:
        return None
    return normalized if normalized.path else None


def _exclude_path_patterns(entries: list[ExcludePathEntry], source_path: str | None) -> list[str]:
    base_path = str(source_path or "").strip()
    if not base_path:
        return []
    try:
        source_root = Path(base_path).expanduser().resolve(strict=False)
    except OSError:
        return []
    patterns: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        normalized = entry.normalized()
        if not normalized.path:
            continue
        try:
            target_path = Path(normalized.path).expanduser().resolve(strict=False)
            relative = target_path.relative_to(source_root)
        except (OSError, ValueError):
            continue
        relative_path = relative.as_posix().strip()
        if not relative_path or relative_path == ".":
            continue
        pattern = f"{relative_path.rstrip('/')}/**" if normalized.kind == "directory" else relative_path
        if pattern not in seen:
            patterns.append(pattern)
            seen.add(pattern)
    return patterns


def _normalize_exclude_pattern(pattern: str) -> str:
    normalized = str(pattern or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _candidate_relative_paths(source_path: str | None, target_path: str | None) -> list[str]:
    def normalize_for_pattern_match(path: str | None, *, resolve: bool) -> str | None:
        raw_path = str(path or "").strip()
        if not raw_path:
            return None
        candidate = Path(raw_path).expanduser()
        try:
            normalized = candidate.resolve(strict=False) if resolve else candidate.absolute()
        except OSError:
            normalized = candidate.absolute()
        value = normalized.as_posix()
        return value.rstrip("/") if value != "/" else value

    relatives: list[str] = []
    for resolve in (False, True):
        normalized_source = normalize_for_pattern_match(source_path, resolve=resolve)
        normalized_target = normalize_for_pattern_match(target_path, resolve=resolve)
        if not normalized_source or not normalized_target:
            continue
        if normalized_target == normalized_source:
            relatives.append(".")
            continue
        if normalized_target.startswith(f"{normalized_source}/"):
            relative = normalized_target[len(normalized_source) + 1 :].strip("/")
            if relative:
                relatives.append(relative)

    parts: list[str] = []
    for relative in relatives:
        parts.append(relative)
        if "/" in relative:
            parts.append(relative.rsplit("/", 1)[-1])
    return list(dict.fromkeys(parts))


def _rclone_exclude_pattern_matches(pattern: str, relative_path: str) -> bool:
    normalized_pattern = _normalize_exclude_pattern(pattern)
    normalized_relative = _normalize_exclude_pattern(relative_path)
    if not normalized_pattern or not normalized_relative:
        return False
    if normalized_pattern.startswith("**/") and _rclone_exclude_pattern_matches(
        normalized_pattern[3:],
        normalized_relative,
    ):
        return True
    if normalized_pattern.endswith("/**"):
        directory = normalized_pattern[:-3].rstrip("/")
        if directory.startswith("**/"):
            suffix = directory[3:]
            return normalized_relative == suffix or normalized_relative.endswith(
                f"/{suffix}"
            ) or f"/{suffix}/" in normalized_relative
        return normalized_relative == directory or normalized_relative.startswith(f"{directory}/")
    if normalized_pattern.endswith("/"):
        directory = normalized_pattern.rstrip("/")
        return normalized_relative == directory or normalized_relative.startswith(f"{directory}/")
    if "/" not in normalized_pattern:
        basename = normalized_relative.rsplit("/", 1)[-1]
        return fnmatchcase(basename, normalized_pattern)
    return fnmatchcase(normalized_relative, normalized_pattern)


def path_is_excluded_from_backup(
    *,
    source_path: str | None,
    target_path: str | None,
    options: BackupOptions,
) -> bool:
    relative_paths = _candidate_relative_paths(source_path, target_path)
    if not relative_paths:
        return False
    normalized_options = options.normalized()
    patterns = [
        *normalized_options.exclude,
        *_exclude_path_patterns(normalized_options.exclude_paths, source_path),
    ]
    return any(
        _rclone_exclude_pattern_matches(pattern, relative_path)
        for pattern in patterns
        for relative_path in relative_paths
    )


def normalize_single_value_flags(argv: list[str]) -> list[str]:
    entries: list[list[str] | None] = []
    latest_positions: dict[str, int] = {}
    index = 0
    while index < len(argv):
        current = str(argv[index])
        singleton_flag = None
        pair: list[str]

        if current in SINGLETON_RCLONE_FLAGS:
            singleton_flag = current
            if index + 1 < len(argv):
                pair = [current, str(argv[index + 1])]
                index += 2
            else:
                pair = [current]
                index += 1
        else:
            for flag in SINGLETON_RCLONE_FLAGS:
                prefix = f"{flag}="
                if current.startswith(prefix):
                    singleton_flag = flag
                    pair = [current]
                    index += 1
                    break
            else:
                pair = [current]
                index += 1

        if singleton_flag is not None:
            previous_position = latest_positions.get(singleton_flag)
            if previous_position is not None:
                entries[previous_position] = None
            latest_positions[singleton_flag] = len(entries)
        entries.append(pair)

    return [item for pair in entries if pair is not None for item in pair]


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
    transfers: int | None = None
    checkers: int | None = None
    tpslimit: float | None = None
    tpslimit_burst: int | None = None
    retries: int | None = None
    low_level_retries: int | None = None
    retries_sleep: str | None = None
    fast_list: bool = False
    no_traverse: bool = False
    debug_dump: str | None = None
    mailru_safe_preset: bool = False
    force_rclone_log: bool = False
    exclude: list[str] = field(default_factory=list)
    exclude_paths: list[ExcludePathEntry] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)

    def normalized(self) -> BackupOptions:
        return BackupOptions(
            max_age=(self.max_age or "").strip() or None,
            min_age=(self.min_age or "").strip() or None,
            transfers=_normalize_optional_int(self.transfers, minimum=1),
            checkers=_normalize_optional_int(self.checkers, minimum=1),
            tpslimit=_normalize_optional_float(self.tpslimit, minimum=0.0),
            tpslimit_burst=_normalize_optional_int(self.tpslimit_burst, minimum=1),
            retries=_normalize_optional_int(self.retries, minimum=0),
            low_level_retries=_normalize_optional_int(self.low_level_retries, minimum=0),
            retries_sleep=_normalize_optional_text(self.retries_sleep),
            fast_list=bool(self.fast_list),
            no_traverse=bool(self.no_traverse),
            debug_dump=_normalize_debug_dump(self.debug_dump),
            mailru_safe_preset=bool(self.mailru_safe_preset),
            force_rclone_log=bool(self.force_rclone_log),
            exclude=[str(item).strip() for item in self.exclude if str(item).strip()],
            exclude_paths=[
                entry
                for raw in self.exclude_paths
                if (entry := _normalize_exclude_path_entry(raw)) is not None
            ],
            extra_args=[str(item).strip() for item in self.extra_args if str(item).strip()],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())

    def to_args(self, *, transfer_mode: str | None = None, source_path: str | None = None) -> list[str]:
        options = self.normalized()
        transfers = options.transfers if options.transfers is not None else (1 if options.mailru_safe_preset else None)
        checkers = options.checkers if options.checkers is not None else (1 if options.mailru_safe_preset else None)
        tpslimit = options.tpslimit if options.tpslimit is not None else (1.0 if options.mailru_safe_preset else None)
        tpslimit_burst = (
            options.tpslimit_burst if options.tpslimit_burst is not None else (1 if options.mailru_safe_preset else None)
        )
        args: list[str] = []
        if options.max_age:
            args.extend(["--max-age", options.max_age])
        if options.min_age:
            args.extend(["--min-age", options.min_age])
        for pattern in [*options.exclude, *_exclude_path_patterns(options.exclude_paths, source_path)]:
            args.extend(["--exclude", pattern])
        _append_singleton_arg(args, "--transfers", transfers)
        _append_singleton_arg(args, "--checkers", checkers)
        _append_singleton_arg(args, "--tpslimit", tpslimit)
        _append_singleton_arg(args, "--tpslimit-burst", tpslimit_burst)
        _append_singleton_arg(args, "--retries", options.retries)
        _append_singleton_arg(args, "--low-level-retries", options.low_level_retries)
        _append_singleton_arg(args, "--retries-sleep", options.retries_sleep)
        if options.fast_list:
            args.append("--fast-list")
        if options.no_traverse and transfer_mode != "sync":
            args.append("--no-traverse")
        if options.debug_dump:
            _append_singleton_arg(args, "--dump", options.debug_dump)
            _append_singleton_arg(args, "--log-level", "DEBUG")
        args.extend(_split_extra_args(options.extra_args))
        return args


@dataclass(frozen=True)
class RetentionSettings:
    enabled: bool = False
    min_age: str | None = None
    transfers: int | None = None
    checkers: int | None = None
    tpslimit: float | None = None
    tpslimit_burst: int | None = None
    retries: int | None = None
    low_level_retries: int | None = None
    retries_sleep: str | None = None
    fast_list: bool = False
    no_traverse: bool = False
    debug_dump: str | None = None
    mailru_safe_preset: bool = False
    exclude: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)

    def normalized(self) -> RetentionSettings:
        return RetentionSettings(
            enabled=bool(self.enabled) and bool((self.min_age or "").strip()),
            min_age=(self.min_age or "").strip() or None,
            transfers=_normalize_optional_int(self.transfers, minimum=1),
            checkers=_normalize_optional_int(self.checkers, minimum=1),
            tpslimit=_normalize_optional_float(self.tpslimit, minimum=0.0),
            tpslimit_burst=_normalize_optional_int(self.tpslimit_burst, minimum=1),
            retries=_normalize_optional_int(self.retries, minimum=0),
            low_level_retries=_normalize_optional_int(self.low_level_retries, minimum=0),
            retries_sleep=_normalize_optional_text(self.retries_sleep),
            fast_list=bool(self.fast_list),
            no_traverse=bool(self.no_traverse),
            debug_dump=_normalize_debug_dump(self.debug_dump),
            mailru_safe_preset=bool(self.mailru_safe_preset),
            exclude=[str(item).strip() for item in self.exclude if str(item).strip()],
            extra_args=[str(item).strip() for item in self.extra_args if str(item).strip()],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())

    def to_args(self) -> list[str]:
        retention = self.normalized()
        transfers = retention.transfers if retention.transfers is not None else (1 if retention.mailru_safe_preset else None)
        checkers = retention.checkers if retention.checkers is not None else (1 if retention.mailru_safe_preset else None)
        tpslimit = retention.tpslimit if retention.tpslimit is not None else (1.0 if retention.mailru_safe_preset else None)
        tpslimit_burst = (
            retention.tpslimit_burst if retention.tpslimit_burst is not None else (1 if retention.mailru_safe_preset else None)
        )
        args: list[str] = []
        if retention.min_age:
            args.extend(["--min-age", retention.min_age])
        for pattern in retention.exclude:
            args.extend(["--exclude", pattern])
        _append_singleton_arg(args, "--transfers", transfers)
        _append_singleton_arg(args, "--checkers", checkers)
        _append_singleton_arg(args, "--tpslimit", tpslimit)
        _append_singleton_arg(args, "--tpslimit-burst", tpslimit_burst)
        _append_singleton_arg(args, "--retries", retention.retries)
        _append_singleton_arg(args, "--low-level-retries", retention.low_level_retries)
        _append_singleton_arg(args, "--retries-sleep", retention.retries_sleep)
        if retention.fast_list:
            args.append("--fast-list")
        if retention.no_traverse:
            args.append("--no-traverse")
        if retention.debug_dump:
            _append_singleton_arg(args, "--dump", retention.debug_dump)
            _append_singleton_arg(args, "--log-level", "DEBUG")
        args.extend(_split_extra_args(retention.extra_args))
        return args


@dataclass(frozen=True)
class ArchiveSettings:
    enabled: bool = False
    filename_template: str = "{key}-{date}.7z"
    date_format: str = "%Y-%m-%d_%H-%M-%S"
    compression_level: int = 5
    temp_dir: str | None = None
    password: str | None = None
    encrypt_headers: bool = False

    def normalized(self) -> ArchiveSettings:
        filename_template = str(self.filename_template or "").strip() or "{key}-{date}.7z"
        if "{date}" not in filename_template:
            stem = filename_template[:-3] if filename_template.lower().endswith(".7z") else filename_template
            filename_template = f"{stem}-{{date}}.7z"
        if not filename_template.lower().endswith(".7z"):
            filename_template = f"{filename_template}.7z"
        return ArchiveSettings(
            enabled=bool(self.enabled),
            filename_template=filename_template,
            date_format=str(self.date_format or "").strip() or "%Y-%m-%d_%H-%M-%S",
            compression_level=max(0, min(9, int(self.compression_level if self.compression_level is not None else 5))),
            temp_dir=(str(self.temp_dir or "").strip() or None),
            password=(str(self.password or "").strip() or None),
            encrypt_headers=bool(self.encrypt_headers),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())


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
    auto_rclone_log_enabled: bool = False
    auto_rclone_log_threshold: int = 3

    def normalized(self) -> LoggingSettings:
        threshold = max(1, min(100, int(self.auto_rclone_log_threshold or 3)))
        return LoggingSettings(
            rclone_log_enabled=bool(self.rclone_log_enabled),
            auto_rclone_log_enabled=bool(self.auto_rclone_log_enabled),
            auto_rclone_log_threshold=threshold,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())


@dataclass(frozen=True)
class WatcherSettings:
    enabled: bool = False
    debounce_seconds: int = 45

    def normalized(self) -> WatcherSettings:
        return WatcherSettings(
            enabled=bool(self.enabled),
            debounce_seconds=max(1, int(self.debounce_seconds or 1)),
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
    serialize_provider_lock: bool = False

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
            serialize_provider_lock=bool(self.serialize_provider_lock),
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
    archive: ArchiveSettings = field(default_factory=ArchiveSettings)
    notifications: JobNotificationSettings = field(default_factory=JobNotificationSettings)
    watcher_enabled: bool = False

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
        archive = self.archive.normalized()
        if kind != "backup":
            archive = ArchiveSettings()
        notifications = self.notifications.normalized()
        watcher_enabled = bool(self.watcher_enabled) and kind == "backup" and bool(source_path)
        description = self.description.strip() or raw_key
        title = (self.title or "").strip() or description
        if kind != "backup" or transfer_mode == "sync":
            retention = RetentionSettings()
        if kind == "backup" and source_path and destination_path:
            if archive.enabled:
                command = self.build_archive_command(
                    source_path=source_path,
                    destination_path=destination_path,
                    archive=archive,
                )
            else:
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
            archive=archive,
            notifications=notifications,
            watcher_enabled=watcher_enabled,
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
            *DEFAULT_RCLONE_ARGS,
            *options.to_args(transfer_mode=transfer_mode, source_path=source_path),
        ]
        return apply_rclone_bwlimit(normalize_single_value_flags(command), bandwidth_limit)

    @staticmethod
    def build_archive_command(
        source_path: str,
        destination_path: str,
        archive: ArchiveSettings,
    ) -> list[str]:
        normalized = archive.normalized()
        command = [
            "python3",
            "-m",
            "app.archive_job",
            "--source",
            source_path,
            "--destination",
            destination_path,
            "--filename-template",
            normalized.filename_template,
            "--date-format",
            normalized.date_format,
            "--compression-level",
            str(normalized.compression_level),
        ]
        if normalized.temp_dir:
            command.extend(["--temp-dir", normalized.temp_dir])
        if normalized.password:
            command.extend(["--password", normalized.password])
        if normalized.encrypt_headers:
            command.append("--encrypt-headers")
        return command

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
            *DEFAULT_RCLONE_ARGS,
            *normalized.to_args(),
        ]
        return apply_rclone_bwlimit(normalize_single_value_flags(command), bandwidth_limit)


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
        watcher: WatcherSettings | None = None,
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
        self.watcher = (watcher or WatcherSettings()).normalized()
        self.replace(
            jobs=jobs,
            profiles=profiles,
            gotify=gotify,
            queues=queues,
            bandwidth=bandwidth,
            logging=logging,
            watcher=watcher,
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
        watcher: WatcherSettings | None = None,
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
            self.watcher = (watcher or self.watcher).normalized()

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
    if raw_limit.isdigit():
        return f"{raw_limit}B"
    return raw_limit


def normalize_local_path(path: str | None) -> str | None:
    raw_path = str(path or "").strip()
    if not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    try:
        normalized = candidate.resolve(strict=False)
    except OSError:
        normalized = candidate.absolute()
    value = normalized.as_posix()
    if value != "/":
        value = value.rstrip("/")
    return value


def path_is_within(root_path: str | None, target_path: str | None) -> bool:
    normalized_root = normalize_local_path(root_path)
    normalized_target = normalize_local_path(target_path)
    if not normalized_root or not normalized_target:
        return False
    if normalized_target == normalized_root:
        return True
    return normalized_target.startswith(f"{normalized_root}/")


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
