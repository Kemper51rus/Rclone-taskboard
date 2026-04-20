from __future__ import annotations

import configparser
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import resource
import threading
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .domain import (
    BackupOptions,
    BandwidthSettings,
    CloudSettings,
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
from .gotify import GotifyClient
from .jobs_loader import build_profiles, load_catalog, save_catalog
from .orchestrator import Orchestrator
from .rclone_metrics import extract_transfer_metrics
from .runner import CommandRunner
from .storage import Storage
from .watcher import FilesystemWatcher


settings: Settings = load_settings()
catalog = load_catalog(
    settings.jobs_file,
    standard_interval_minutes=settings.standard_interval_minutes,
    heavy_hour=settings.heavy_hour,
    watcher_debounce_seconds=settings.watcher_debounce_seconds,
)
catalog_lock = threading.RLock()
storage = Storage(settings.db_path)
runner = CommandRunner(
    dry_run=settings.dry_run,
    output_tail_chars=settings.output_tail_chars,
)
gotify = GotifyClient()
orchestrator = Orchestrator(
    settings=settings,
    storage=storage,
    catalog=catalog,
    runner=runner,
    gotify=gotify,
)
event_watcher = FilesystemWatcher(
    catalog=catalog,
    on_event=orchestrator.enqueue_event,
)
DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
APP_LOGO_PATH = Path(__file__).with_name("rclone-taskboard-logo.svg")
FS_ROOTS = ["/media", "/srv", "/home", "/root", "/mnt", "/tmp"]
RUN_HISTORY_RETENTION_DAYS = 365
RUN_HISTORY_LAST_PRUNED_AT_STATE_KEY = "run_history_last_pruned_at"
PROCESS_STARTED_AT = datetime.now(timezone.utc)
PROCESS_STARTED_MONOTONIC = time.monotonic()
STATS_PERIODS = {
    "day": ("За день", timedelta(days=1)),
    "week": ("За неделю", timedelta(days=7)),
    "month": ("За месяц", timedelta(days=30)),
    "year": ("За год", timedelta(days=365)),
}
HOMEPAGE_SLOW_CACHE_SECONDS = 5.0
homepage_slow_cache_lock = threading.RLock()
homepage_slow_cache: dict[str, Any] = {
    "expires_at": 0.0,
    "payload": None,
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    storage.initialize()
    storage.recover_incomplete_runs()
    orchestrator.start()
    event_watcher.start()
    try:
        yield
    finally:
        event_watcher.stop()
        orchestrator.stop()


app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    lifespan=lifespan,
)


def _get_bearer_token(header_value: str | None) -> str | None:
    if not header_value:
        return None
    parts = header_value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def require_write_access(request: Request) -> None:
    return


def _rclone_logs_dir() -> Path:
    return settings.app_root / "data" / "rclone-logs"


def _step_rclone_log_path(run_id: int, step_id: int) -> Path:
    return _rclone_logs_dir() / f"run-{run_id}-step-{step_id}.log"


def _relative_app_path(path: Path) -> str:
    try:
        return path.relative_to(settings.app_root).as_posix()
    except ValueError:
        return path.as_posix()


def _read_log_tail(path: Path, lines: int) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _current_rss_bytes() -> int | None:
    status_path = Path("/proc/self/status")
    try:
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except OSError:
        return None
    return None


def _open_fd_count() -> int | None:
    try:
        return len(list(Path("/proc/self/fd").iterdir()))
    except OSError:
        return None


def _process_diagnostics() -> dict[str, Any]:
    try:
        fd_soft_limit, fd_hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        fd_soft_limit = None
        fd_hard_limit = None
    uptime_seconds = max(0, int(time.monotonic() - PROCESS_STARTED_MONOTONIC))
    return {
        "pid": os.getpid(),
        "started_at": PROCESS_STARTED_AT.isoformat(),
        "uptime_seconds": uptime_seconds,
        "open_fds": _open_fd_count(),
        "fd_soft_limit": fd_soft_limit,
        "fd_hard_limit": fd_hard_limit,
        "rss_bytes": _current_rss_bytes(),
    }


def _system_diagnostics() -> dict[str, Any]:
    return {
        "database": storage.database_diagnostics(),
        "process": _process_diagnostics(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _homepage_slow_snapshot() -> dict[str, Any]:
    now = time.monotonic()
    with homepage_slow_cache_lock:
        cached_payload = homepage_slow_cache.get("payload")
        if isinstance(cached_payload, dict) and now < float(homepage_slow_cache.get("expires_at") or 0):
            return cached_payload

        database = storage.database_diagnostics()
        jobs = catalog.raw_jobs()
        payload = {
            "jobs_total": len(jobs),
            "enabled_jobs_total": sum(1 for job in jobs if job.enabled),
            "database_size_bytes": database.get("database_size_bytes", 0),
            "database_total_size_bytes": database.get("total_size_bytes", 0),
            "database_wal_size_bytes": database.get("wal_size_bytes", 0),
            "database_reclaimable_bytes": database.get("reclaimable_bytes", 0),
            "database_journal_mode": database.get("journal_mode"),
        }
        homepage_slow_cache["payload"] = payload
        homepage_slow_cache["expires_at"] = now + HOMEPAGE_SLOW_CACHE_SECONDS
        return payload


def _homepage_snapshot() -> dict[str, Any]:
    snapshot = orchestrator.snapshot()
    generated_at = datetime.now(timezone.utc)
    return {
        "status": "ok",
        "app": settings.app_name,
        "generated_at": generated_at.isoformat(),
        "slow_cache_seconds": int(HOMEPAGE_SLOW_CACHE_SECONDS),
        "open_runs_total": snapshot.get("open_runs_total", 0),
        "standard_queue_size": snapshot.get("standard_queue_size", 0),
        "heavy_queue_size": snapshot.get("heavy_queue_size", 0),
        "total_copy_speed_bytes_per_second": snapshot.get("total_copy_speed_bytes_per_second", 0),
        "total_copy_speed_megabits_per_second": snapshot.get("total_copy_speed_megabits_per_second", 0),
        **_homepage_slow_snapshot(),
    }


def _is_rclone_step(step: dict[str, Any]) -> bool:
    command = step.get("command") or []
    return bool(command) and str(command[0]).strip() == "rclone"


def _serialize_rclone_log_item(step: dict[str, Any]) -> dict[str, Any]:
    run_id = int(step["run_id"])
    step_id = int(step["id"])
    log_path = _step_rclone_log_path(run_id=run_id, step_id=step_id)
    log_exists = log_path.exists() and log_path.is_file()
    log_stat = log_path.stat() if log_exists else None
    job_key = str(step.get("job_key") or "").strip()
    job = catalog.get_job(job_key) if job_key else None
    title = (
        (job.title or job.description or job.key)
        if job
        else str(step.get("description") or job_key or f"step #{step_id}")
    )
    return {
        "step_id": step_id,
        "run_id": run_id,
        "step_order": int(step.get("step_order") or 0),
        "job_key": job_key or None,
        "title": title,
        "description": step.get("description"),
        "status": step.get("status"),
        "exit_code": step.get("exit_code"),
        "profile": step.get("run_profile"),
        "run_status": step.get("run_status"),
        "trigger_type": step.get("run_trigger_type"),
        "requested_at": step.get("run_requested_at"),
        "run_started_at": step.get("run_started_at"),
        "run_finished_at": step.get("run_finished_at"),
        "started_at": step.get("started_at"),
        "finished_at": step.get("finished_at"),
        "duration_seconds": step.get("duration_seconds"),
        "log_mode": step.get("log_mode"),
        "log_path": _relative_app_path(log_path),
        "log_available": log_exists,
        "log_size_bytes": int(log_stat.st_size) if log_stat else 0,
        "log_updated_at": (
            datetime.fromtimestamp(log_stat.st_mtime, timezone.utc).isoformat()
            if log_stat
            else None
        ),
    }


def _statistics_period_bounds(period: str) -> tuple[str, datetime]:
    normalized = str(period or "week").strip().lower()
    label, delta = STATS_PERIODS.get(normalized, STATS_PERIODS["week"])
    started_at = datetime.now(timezone.utc) - delta
    return label, started_at


def _statistics_summary(period: str) -> dict[str, Any]:
    period_key = str(period or "week").strip().lower()
    period_label, started_at = _statistics_period_bounds(period_key)
    started_at_iso = started_at.isoformat()
    runs = storage.stats_run_counts_since(started_at_iso)
    steps = storage.list_statistics_steps(started_at_iso)
    traffic_bytes = 0
    files_total = 0
    transfer_duration_seconds = 0.0
    sampled_steps = 0
    sampled_file_steps = 0

    for step in steps:
        command = step.get("command") or []
        if not command or str(command[0]).strip() != "rclone":
            continue

        transferred_bytes = step.get("transferred_bytes")
        total_bytes = step.get("total_bytes")
        file_count = step.get("file_count")
        file_total = step.get("file_total")
        if any(value is None for value in (transferred_bytes, total_bytes, file_count, file_total)):
            parsed = extract_transfer_metrics(
                progress=step.get("progress"),
                log_path=(
                    _step_rclone_log_path(run_id=int(step["run_id"]), step_id=int(step["id"]))
                    if step.get("log_mode")
                    else None
                ),
                started_at_raw=step.get("started_at"),
                timezone_name=settings.timezone,
            )
            transferred_bytes = transferred_bytes if transferred_bytes is not None else parsed["transferred_bytes"]
            total_bytes = total_bytes if total_bytes is not None else parsed["total_bytes"]
            file_count = file_count if file_count is not None else parsed["file_count"]
            file_total = file_total if file_total is not None else parsed["file_total"]
            if any(value is not None for value in (transferred_bytes, total_bytes, file_count, file_total)):
                storage.update_step_statistics(
                    int(step["id"]),
                    transferred_bytes=transferred_bytes,
                    total_bytes=total_bytes,
                    file_count=file_count,
                    file_total=file_total,
                )

        if transferred_bytes is not None and transferred_bytes > 0:
            traffic_bytes += int(transferred_bytes)
            duration = float(step.get("duration_seconds") or 0)
            if duration > 0:
                transfer_duration_seconds += duration
            sampled_steps += 1
        elif total_bytes is not None and total_bytes > 0:
            sampled_steps += 1

        effective_file_count = file_count if file_count is not None else file_total
        if effective_file_count is not None and effective_file_count > 0:
            files_total += int(effective_file_count)
            sampled_file_steps += 1

    average_speed = int(traffic_bytes / transfer_duration_seconds) if transfer_duration_seconds > 0 else 0
    last_pruned_at = storage.get_state(RUN_HISTORY_LAST_PRUNED_AT_STATE_KEY)
    return {
        "period": period_key if period_key in STATS_PERIODS else "week",
        "period_label": period_label,
        "started_at": started_at_iso,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "runs": runs,
        "transfer": {
            "traffic_bytes": traffic_bytes,
            "files": files_total,
            "average_speed_bytes_per_second": average_speed,
            "sampled_steps": sampled_steps,
            "sampled_file_steps": sampled_file_steps,
        },
        "retention": {
            "history_days": RUN_HISTORY_RETENTION_DAYS,
            "last_pruned_at": last_pruned_at,
        },
        "system": _system_diagnostics(),
    }


class RunCreateRequest(BaseModel):
    profile: str = Field(default="standard")
    source: str = Field(default="api")
    requested_by: str = Field(default="api")
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventTriggerRequest(BaseModel):
    event_type: str = Field(default="filesystem")
    path: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RunStepControlPayload(BaseModel):
    action: str = Field(pattern="^(pause|resume|stop)$")


class SchedulePayload(BaseModel):
    enabled: bool = False
    mode: str = "manual"
    interval_minutes: int = 60
    hour: int = 3
    minute: int = 0
    weekdays: list[int] = Field(default_factory=list)


class ExcludePathEntryPayload(BaseModel):
    path: str
    kind: str = "directory"


class BackupOptionsPayload(BaseModel):
    max_age: str | None = None
    min_age: str | None = None
    transfers: int | None = Field(default=None, ge=1)
    checkers: int | None = Field(default=None, ge=1)
    tpslimit: float | None = Field(default=None, ge=0)
    tpslimit_burst: int | None = Field(default=None, ge=1)
    retries: int | None = Field(default=None, ge=0)
    low_level_retries: int | None = Field(default=None, ge=0)
    retries_sleep: str | None = None
    fast_list: bool = False
    no_traverse: bool = False
    debug_dump: str | None = None
    mailru_safe_preset: bool = False
    force_rclone_log: bool = False
    exclude: list[str] = Field(default_factory=list)
    exclude_paths: list[ExcludePathEntryPayload] = Field(default_factory=list)
    extra_args: list[str] = Field(default_factory=list)


class JobNotificationPayload(BaseModel):
    on_success: bool = False
    on_failure: bool = True
    priority: int | None = None
    custom_title: str | None = None


class RetentionPayload(BaseModel):
    enabled: bool = False
    min_age: str | None = None
    transfers: int | None = Field(default=None, ge=1)
    checkers: int | None = Field(default=None, ge=1)
    tpslimit: float | None = Field(default=None, ge=0)
    tpslimit_burst: int | None = Field(default=None, ge=1)
    retries: int | None = Field(default=None, ge=0)
    low_level_retries: int | None = Field(default=None, ge=0)
    retries_sleep: str | None = None
    fast_list: bool = False
    no_traverse: bool = False
    debug_dump: str | None = None
    mailru_safe_preset: bool = False
    exclude: list[str] = Field(default_factory=list)
    extra_args: list[str] = Field(default_factory=list)


class BackupJobPayload(BaseModel):
    key: str
    description: str | None = None
    title: str | None = None
    profile: str = "standard"
    enabled: bool = True
    timeout_seconds: int = 1800
    continue_on_error: bool = True
    source_path: str
    cloud_key: str | None = None
    destination_subpath: str | None = None
    destination_path: str
    transfer_mode: str = "copy"
    schedule: SchedulePayload = Field(default_factory=SchedulePayload)
    options: BackupOptionsPayload = Field(default_factory=BackupOptionsPayload)
    retention: RetentionPayload = Field(default_factory=RetentionPayload)
    notifications: JobNotificationPayload = Field(default_factory=JobNotificationPayload)
    watcher_enabled: bool = False
    order: int = 10


class JobPayload(BaseModel):
    key: str
    description: str | None = None
    title: str | None = None
    kind: str = "backup"
    profile: str = "standard"
    enabled: bool = True
    timeout_seconds: int = 1800
    continue_on_error: bool = True
    source_path: str | None = None
    cloud_key: str | None = None
    destination_subpath: str | None = None
    destination_path: str | None = None
    transfer_mode: str = "copy"
    command: list[str] = Field(default_factory=list)
    schedule: SchedulePayload = Field(default_factory=SchedulePayload)
    options: BackupOptionsPayload = Field(default_factory=BackupOptionsPayload)
    retention: RetentionPayload = Field(default_factory=RetentionPayload)
    notifications: JobNotificationPayload = Field(default_factory=JobNotificationPayload)
    watcher_enabled: bool = False
    order: int = 10


class JobCatalogPayload(BaseModel):
    jobs: list[JobPayload] = Field(default_factory=list)


class BackupCatalogPayload(BaseModel):
    jobs: list[BackupJobPayload] = Field(default_factory=list)


class GotifyPayload(BaseModel):
    enabled: bool = False
    url: str | None = None
    token: str | None = None
    default_priority: int = 5


class QueueSettingsPayload(BaseModel):
    allow_parallel_profiles: bool = False
    allow_scheduler_queueing: bool = False
    allow_event_queueing: bool = False
    definitions: list["QueueDefinitionPayload"] = Field(default_factory=list)


class QueueDefinitionPayload(BaseModel):
    key: str
    title: str | None = None
    workers: int = 1
    bandwidth_limit: str | None = None
    enabled: bool = True


class BandwidthPayload(BaseModel):
    limit: str | None = None


class LoggingPayload(BaseModel):
    rclone_log_enabled: bool = False
    auto_rclone_log_enabled: bool = False
    auto_rclone_log_threshold: int = Field(default=3, ge=1, le=100)


class SchedulerPayload(BaseModel):
    enabled: bool = True


class AntiBotPayload(BaseModel):
    enabled: bool = True


class CloudLockPayload(BaseModel):
    serialize_provider_lock: bool = False


class WatcherPayload(BaseModel):
    enabled: bool = False
    debounce_seconds: int = 45


def _slug_cloud_key(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "_" for char in value.strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned or "cloud"


def _import_clouds_from_rclone_config(
    config_path: Path,
    existing_clouds: list[CloudSettings],
) -> list[CloudSettings]:
    if not config_path.exists():
        raise FileNotFoundError(f"rclone config not found: {config_path}")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")

    clouds_by_key = {cloud.key: cloud for cloud in existing_clouds}
    key_by_remote = {
        cloud.remote_name: cloud.key
        for cloud in existing_clouds
        if cloud.remote_name
    }
    known_option_names = {
        "type",
        "vendor",
        "user",
        "username",
        "login",
        "email",
        "token",
        "access_token",
        "bearer_token",
        "password",
        "pass",
        "endpoint",
        "url",
        "server",
        "hostname",
        "root_path",
        "root_folder",
        "root_folder_id",
        "directory",
    }

    for section in parser.sections():
        remote_name = section.strip()
        if not remote_name:
            continue
        values = parser[section]
        provider = (
            values.get("vendor")
            or values.get("type")
            or "generic"
        ).strip().lower()
        username = (
            values.get("user")
            or values.get("username")
            or values.get("login")
            or values.get("email")
        )
        token = (
            values.get("token")
            or values.get("access_token")
            or values.get("bearer_token")
            or values.get("password")
            or values.get("pass")
        )
        endpoint = (
            values.get("endpoint")
            or values.get("url")
            or values.get("server")
            or values.get("hostname")
        )
        root_path = (
            values.get("root_path")
            or values.get("root_folder")
            or values.get("root_folder_id")
            or values.get("directory")
        )
        extra_config = {
            option_key: option_value
            for option_key, option_value in values.items()
            if option_key not in known_option_names and str(option_value).strip()
        }
        existing_key = key_by_remote.get(remote_name)
        existing = clouds_by_key.get(existing_key) if existing_key else None
        key = existing.key if existing else _slug_cloud_key(remote_name)
        while key in clouds_by_key and clouds_by_key[key].remote_name != remote_name:
            key = _slug_cloud_key(f"{remote_name}_{len(clouds_by_key) + 1}")
        title = existing.title if existing and existing.title else remote_name
        notes = existing.notes if existing and existing.notes else f"Imported from {config_path.name}"
        clouds_by_key[key] = CloudSettings(
            key=key,
            title=title,
            provider=provider,
            remote_name=remote_name,
            username=(username if username not in (None, "") else (existing.username if existing else None)),
            token=(token if token not in (None, "") else (existing.token if existing else None)),
            endpoint=(endpoint if endpoint not in (None, "") else (existing.endpoint if existing else None)),
            root_path=(root_path if root_path not in (None, "") else (existing.root_path if existing else None)),
            notes=notes,
            extra_config=(extra_config or (existing.extra_config if existing else {})),
            enabled=existing.enabled if existing else True,
            serialize_provider_lock=existing.serialize_provider_lock if existing else False,
        ).normalized()
        key_by_remote[remote_name] = key

    return sorted(clouds_by_key.values(), key=lambda cloud: (cloud.title, cloud.key))


def _compose_cloud_destination(cloud: CloudSettings | None, destination_subpath: str | None) -> str | None:
    if not cloud or not cloud.remote_name:
        return None
    root_path = (cloud.root_path or "").strip().strip("/")
    subpath = (destination_subpath or "").strip().strip("/")
    segments = [segment for segment in [root_path, subpath] if segment]
    if not segments:
        return f"{cloud.remote_name}:"
    return f"{cloud.remote_name}:/{'/'.join(segments)}"


def _refresh_catalog_clouds_from_rclone() -> list[CloudSettings]:
    with catalog_lock:
        current_jobs = catalog.raw_jobs()
        current_profiles = catalog.profiles
        current_gotify = catalog.gotify
        current_queues = catalog.queues
        current_watcher = catalog.watcher
        current_clouds = catalog.raw_clouds()
        try:
            refreshed_clouds = _import_clouds_from_rclone_config(
                settings.rclone_config_file,
                current_clouds,
            )
        except FileNotFoundError:
            refreshed_clouds = []
        except Exception:
            refreshed_clouds = current_clouds

        catalog.replace(
            current_jobs,
            current_profiles,
            gotify=current_gotify,
            queues=current_queues,
            bandwidth=catalog.bandwidth,
            logging=catalog.logging,
            watcher=current_watcher,
            clouds=refreshed_clouds,
        )
        return catalog.raw_clouds()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/favicon.svg")
def favicon() -> FileResponse:
    return FileResponse(APP_LOGO_PATH, media_type="image/svg+xml")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "utc_time": datetime.now(timezone.utc).isoformat(),
        "app": settings.app_name,
    }


@app.get("/api/homepage")
def homepage_snapshot() -> dict[str, Any]:
    return _homepage_snapshot()


@app.get("/api/state")
def state() -> dict[str, Any]:
    snapshot = orchestrator.snapshot()
    snapshot["token_required"] = bool(settings.api_token)
    snapshot["latest_runs"] = storage.list_runs(limit=999)
    snapshot["latest_job_runs"] = storage.latest_job_run_map()
    snapshot["backup_jobs"] = catalog.list_backup_jobs()
    snapshot["watcher"] = event_watcher.snapshot()
    snapshot["system"] = _system_diagnostics()
    return snapshot


@app.get("/api/stats/summary")
def stats_summary(period: str = Query(default="week")) -> dict[str, Any]:
    return _statistics_summary(period)


@app.get("/api/system")
def system_diagnostics() -> dict[str, Any]:
    return _system_diagnostics()


@app.post("/api/system/database/checkpoint", dependencies=[Depends(require_write_access)])
def checkpoint_database() -> dict[str, Any]:
    return storage.checkpoint_database()


@app.post("/api/system/database/vacuum", dependencies=[Depends(require_write_access)])
def vacuum_database() -> dict[str, Any]:
    return storage.vacuum_database()


@app.get("/api/jobs")
def jobs() -> dict[str, Any]:
    clouds = _refresh_catalog_clouds_from_rclone()
    latest_runs_by_job = storage.latest_job_run_map()
    jobs_payload = catalog.list_jobs()
    for item in jobs_payload:
        latest_run = latest_runs_by_job.get(str(item.get("key", "")), {})
        item["last_run"] = latest_run or None
        item["last_run_status"] = latest_run.get("status") if latest_run else None
        item["last_run_started_at"] = latest_run.get("started_at") if latest_run else None
        item["last_run_requested_at"] = latest_run.get("requested_at") if latest_run else None
    return {
        "profiles": catalog.profiles,
        "gotify": catalog.gotify.to_dict(),
        "queues": catalog.queues.to_dict(),
        "bandwidth": catalog.bandwidth.to_dict(),
        "logging": catalog.logging.to_dict(),
        "watcher": catalog.watcher.to_dict(),
        "clouds": [cloud.to_dict() for cloud in clouds],
        "jobs": jobs_payload,
        "backup_jobs": catalog.list_backup_jobs(),
        "command_jobs": catalog.list_command_jobs(),
    }


@app.get("/api/gotify")
def get_gotify_settings() -> dict[str, Any]:
    return {"gotify": catalog.gotify.to_dict()}


@app.get("/api/queues")
def get_queue_settings() -> dict[str, Any]:
    return {"queues": catalog.queues.to_dict()}


@app.get("/api/bandwidth")
def get_bandwidth_settings() -> dict[str, Any]:
    return {"bandwidth": catalog.bandwidth.to_dict()}


@app.get("/api/logging")
def get_logging_settings() -> dict[str, Any]:
    return {"logging": catalog.logging.to_dict()}


@app.get("/api/watcher")
def get_watcher_settings() -> dict[str, Any]:
    return {
        "watcher": catalog.watcher.to_dict(),
        "runtime": event_watcher.snapshot(),
    }


@app.get("/api/logging/rclone-tail")
def get_rclone_log_tail(lines: int = Query(default=100, ge=1, le=2000)) -> dict[str, Any]:
    logs_dir = _rclone_logs_dir()
    if not logs_dir.exists() or not logs_dir.is_dir():
        return {"path": None, "lines": lines, "content": "", "available": False}

    candidates = [path for path in logs_dir.glob("*.log") if path.is_file()]
    if not candidates:
        return {"path": None, "lines": lines, "content": "", "available": False}

    latest_path = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        content = _read_log_tail(latest_path, lines)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to read log: {exc}") from exc

    return {
        "path": _relative_app_path(latest_path),
        "lines": lines,
        "content": content,
        "available": True,
    }


@app.get("/api/logging/rclone-files")
def list_rclone_log_files(
    limit: int = Query(default=200, ge=1, le=1000),
    job_key: str | None = None,
    status: str | None = None,
    trigger_type: str | None = None,
    run_id: int | None = None,
    only_with_log: bool = False,
    only_errors: bool = False,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for step in storage.list_rclone_log_steps(limit=limit):
        item = _serialize_rclone_log_item(step)
        if job_key and item.get("job_key") != job_key:
            continue
        if status and item.get("status") != status:
            continue
        if trigger_type and item.get("trigger_type") != trigger_type:
            continue
        if run_id is not None and int(item.get("run_id") or 0) != run_id:
            continue
        if only_with_log and not item.get("log_available"):
            continue
        if only_errors and item.get("status") not in {"failed", "stopped"}:
            continue
        items.append(item)
    return {"logs": items, "count": len(items)}


@app.get("/api/logging/rclone-files/{step_id}")
def get_rclone_log_file(step_id: int) -> dict[str, Any]:
    step = storage.get_run_step(step_id)
    if step is None or not _is_rclone_step(step):
        raise HTTPException(status_code=404, detail="rclone log step not found")
    path = _step_rclone_log_path(run_id=int(step["run_id"]), step_id=step_id)
    if not path.exists() or not path.is_file():
        return {
            "step_id": step_id,
            "run_id": int(step["run_id"]),
            "path": _relative_app_path(path),
            "content": "",
            "available": False,
            "log_mode": step.get("log_mode"),
        }
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to read log: {exc}") from exc
    return {
        "step_id": step_id,
        "run_id": int(step["run_id"]),
        "path": _relative_app_path(path),
        "content": content,
        "available": True,
        "log_mode": step.get("log_mode"),
    }


@app.delete("/api/logging/rclone-log", dependencies=[Depends(require_write_access)])
def clear_rclone_logs() -> dict[str, Any]:
    logs_dir = _rclone_logs_dir()
    if not logs_dir.exists() or not logs_dir.is_dir():
        return {"cleared": True, "files": 0}

    files_cleared = 0
    for log_path in logs_dir.glob("*.log"):
        if not log_path.is_file():
            continue
        try:
            log_path.write_text("", encoding="utf-8")
            files_cleared += 1
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"failed to clear log {log_path.name}: {exc}",
            ) from exc

    return {"cleared": True, "files": files_cleared}


@app.delete("/api/logging/rclone-files/{step_id}", dependencies=[Depends(require_write_access)])
def clear_rclone_log_file(step_id: int) -> dict[str, Any]:
    step = storage.get_run_step(step_id)
    if step is None or not _is_rclone_step(step):
        raise HTTPException(status_code=404, detail="rclone log step not found")
    path = _step_rclone_log_path(run_id=int(step["run_id"]), step_id=step_id)
    if not path.exists() or not path.is_file():
        return {"cleared": True, "step_id": step_id, "available": False}
    try:
        path.write_text("", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to clear log: {exc}") from exc
    return {
        "cleared": True,
        "step_id": step_id,
        "available": True,
        "path": _relative_app_path(path),
    }


@app.get("/api/clouds")
def get_cloud_settings() -> dict[str, Any]:
    clouds = _refresh_catalog_clouds_from_rclone()
    return {"clouds": [cloud.to_dict() for cloud in clouds]}


@app.put("/api/clouds/{cloud_key}/lock", dependencies=[Depends(require_write_access)])
def update_cloud_lock_settings(cloud_key: str, payload: CloudLockPayload) -> dict[str, Any]:
    clouds = _refresh_catalog_clouds_from_rclone()
    current = next((cloud for cloud in clouds if cloud.key == cloud_key), None)
    if current is None:
        raise HTTPException(status_code=404, detail="cloud not found")

    updated_cloud = CloudSettings(
        key=current.key,
        title=current.title,
        provider=current.provider,
        remote_name=current.remote_name,
        username=current.username,
        token=current.token,
        endpoint=current.endpoint,
        root_path=current.root_path,
        notes=current.notes,
        extra_config=current.extra_config,
        enabled=current.enabled,
        serialize_provider_lock=payload.serialize_provider_lock,
    ).normalized()
    updated_clouds = [
        updated_cloud if cloud.key == cloud_key else cloud
        for cloud in clouds
    ]

    with catalog_lock:
        updated_catalog = JobCatalog(
            jobs=catalog.raw_jobs(),
            profiles=build_profiles(catalog.raw_jobs(), queue_keys=catalog.queues.queue_keys()),
            gotify=catalog.gotify,
            queues=catalog.queues,
            bandwidth=catalog.bandwidth,
            logging=catalog.logging,
            watcher=catalog.watcher,
            clouds=updated_clouds,
        )
        save_catalog(settings.jobs_file, updated_catalog)
        catalog.replace(
            updated_catalog.raw_jobs(),
            updated_catalog.profiles,
            gotify=updated_catalog.gotify,
            queues=updated_catalog.queues,
            bandwidth=updated_catalog.bandwidth,
            logging=updated_catalog.logging,
            watcher=updated_catalog.watcher,
            clouds=updated_catalog.raw_clouds(),
        )

    return {
        "saved": True,
        "cloud": updated_cloud.to_dict(),
        "clouds": catalog.list_clouds(),
    }


@app.put("/api/gotify", dependencies=[Depends(require_write_access)])
def update_gotify_settings(payload: GotifyPayload) -> dict[str, Any]:
    gotify = GotifySettings(**payload.model_dump()).normalized()
    with catalog_lock:
        updated_catalog = JobCatalog(
            jobs=catalog.raw_jobs(),
            profiles=build_profiles(catalog.raw_jobs(), queue_keys=catalog.queues.queue_keys()),
            gotify=gotify,
            queues=catalog.queues,
            bandwidth=catalog.bandwidth,
            logging=catalog.logging,
            watcher=catalog.watcher,
            clouds=catalog.raw_clouds(),
        )
        save_catalog(settings.jobs_file, updated_catalog)
        catalog.replace(
            updated_catalog.raw_jobs(),
            updated_catalog.profiles,
            gotify=updated_catalog.gotify,
            queues=updated_catalog.queues,
            bandwidth=updated_catalog.bandwidth,
            logging=updated_catalog.logging,
            watcher=updated_catalog.watcher,
            clouds=updated_catalog.raw_clouds(),
        )
    return {"saved": True, "gotify": catalog.gotify.to_dict()}


@app.put("/api/queues", dependencies=[Depends(require_write_access)])
def update_queue_settings(payload: QueueSettingsPayload) -> dict[str, Any]:
    queues = QueueSettings(
        allow_parallel_profiles=payload.allow_parallel_profiles,
        allow_scheduler_queueing=payload.allow_scheduler_queueing,
        allow_event_queueing=payload.allow_event_queueing,
        definitions=[
            QueueDefinition(
                key=item.key,
                title=item.title,
                workers=item.workers,
                bandwidth_limit=item.bandwidth_limit,
                enabled=item.enabled,
            )
            for item in payload.definitions
        ],
    ).normalized()
    queue_keys = {definition.key for definition in queues.definitions}
    for job in catalog.raw_jobs():
        if job.profile not in queue_keys:
            raise HTTPException(
                status_code=400,
                detail=f"job '{job.key}' references missing queue '{job.profile}'",
            )
    with catalog_lock:
        updated_catalog = JobCatalog(
            jobs=catalog.raw_jobs(),
            profiles=build_profiles(catalog.raw_jobs(), queue_keys=queues.queue_keys()),
            gotify=catalog.gotify,
            queues=queues,
            bandwidth=catalog.bandwidth,
            logging=catalog.logging,
            watcher=catalog.watcher,
            clouds=catalog.raw_clouds(),
        )
        save_catalog(settings.jobs_file, updated_catalog)
        catalog.replace(
            updated_catalog.raw_jobs(),
            updated_catalog.profiles,
            gotify=updated_catalog.gotify,
            queues=updated_catalog.queues,
            bandwidth=updated_catalog.bandwidth,
            logging=updated_catalog.logging,
            watcher=updated_catalog.watcher,
            clouds=updated_catalog.raw_clouds(),
        )
        orchestrator.sync_workers_from_catalog()
    return {"saved": True, "queues": catalog.queues.to_dict()}


@app.put("/api/bandwidth", dependencies=[Depends(require_write_access)])
def update_bandwidth_settings(payload: BandwidthPayload) -> dict[str, Any]:
    bandwidth = BandwidthSettings(**payload.model_dump()).normalized()
    with catalog_lock:
        updated_catalog = JobCatalog(
            jobs=catalog.raw_jobs(),
            profiles=build_profiles(catalog.raw_jobs(), queue_keys=catalog.queues.queue_keys()),
            gotify=catalog.gotify,
            queues=catalog.queues,
            bandwidth=bandwidth,
            logging=catalog.logging,
            watcher=catalog.watcher,
            clouds=catalog.raw_clouds(),
        )
        save_catalog(settings.jobs_file, updated_catalog)
        catalog.replace(
            updated_catalog.raw_jobs(),
            updated_catalog.profiles,
            gotify=updated_catalog.gotify,
            queues=updated_catalog.queues,
            bandwidth=updated_catalog.bandwidth,
            logging=updated_catalog.logging,
            watcher=updated_catalog.watcher,
            clouds=updated_catalog.raw_clouds(),
        )
    return {"saved": True, "bandwidth": catalog.bandwidth.to_dict()}


@app.put("/api/logging", dependencies=[Depends(require_write_access)])
def update_logging_settings(payload: LoggingPayload) -> dict[str, Any]:
    logging_settings = LoggingSettings(**payload.model_dump()).normalized()
    with catalog_lock:
        updated_catalog = JobCatalog(
            jobs=catalog.raw_jobs(),
            profiles=build_profiles(catalog.raw_jobs(), queue_keys=catalog.queues.queue_keys()),
            gotify=catalog.gotify,
            queues=catalog.queues,
            bandwidth=catalog.bandwidth,
            logging=logging_settings,
            watcher=catalog.watcher,
            clouds=catalog.raw_clouds(),
        )
        save_catalog(settings.jobs_file, updated_catalog)
        catalog.replace(
            updated_catalog.raw_jobs(),
            updated_catalog.profiles,
            gotify=updated_catalog.gotify,
            queues=updated_catalog.queues,
            bandwidth=updated_catalog.bandwidth,
            logging=updated_catalog.logging,
            watcher=updated_catalog.watcher,
            clouds=updated_catalog.raw_clouds(),
        )
    return {"saved": True, "logging": catalog.logging.to_dict()}


@app.put("/api/scheduler", dependencies=[Depends(require_write_access)])
def update_scheduler_settings(payload: SchedulerPayload) -> dict[str, Any]:
    return {
        "saved": True,
        **orchestrator.set_scheduler_enabled(payload.enabled),
    }


@app.put("/api/antibot", dependencies=[Depends(require_write_access)])
def update_antibot_settings(payload: AntiBotPayload) -> dict[str, Any]:
    return {
        "saved": True,
        **orchestrator.set_antibot_enabled(payload.enabled),
    }


@app.put("/api/watcher", dependencies=[Depends(require_write_access)])
def update_watcher_settings(payload: WatcherPayload) -> dict[str, Any]:
    watcher_settings = WatcherSettings(**payload.model_dump()).normalized()
    with catalog_lock:
        updated_catalog = JobCatalog(
            jobs=catalog.raw_jobs(),
            profiles=build_profiles(catalog.raw_jobs(), queue_keys=catalog.queues.queue_keys()),
            gotify=catalog.gotify,
            queues=catalog.queues,
            bandwidth=catalog.bandwidth,
            logging=catalog.logging,
            watcher=watcher_settings,
            clouds=catalog.raw_clouds(),
        )
        save_catalog(settings.jobs_file, updated_catalog)
        catalog.replace(
            updated_catalog.raw_jobs(),
            updated_catalog.profiles,
            gotify=updated_catalog.gotify,
            queues=updated_catalog.queues,
            bandwidth=updated_catalog.bandwidth,
            logging=updated_catalog.logging,
            watcher=updated_catalog.watcher,
            clouds=updated_catalog.raw_clouds(),
        )
        event_watcher.sync_from_catalog()
    return {
        "saved": True,
        "watcher": catalog.watcher.to_dict(),
        "runtime": event_watcher.snapshot(),
    }


@app.put("/api/clouds", dependencies=[Depends(require_write_access)])
def update_cloud_settings() -> dict[str, Any]:
    raise HTTPException(
        status_code=403,
        detail="cloud settings are read-only and sourced from rclone.conf",
    )


@app.post("/api/clouds/import-rclone", dependencies=[Depends(require_write_access)])
def import_cloud_settings_from_rclone() -> dict[str, Any]:
    raise HTTPException(
        status_code=403,
        detail="cloud import is no longer available in the UI; use rclone.conf directly",
    )


@app.post("/api/clouds/import-rclone-remote", dependencies=[Depends(require_write_access)])
def import_single_cloud_settings_from_rclone() -> dict[str, Any]:
    raise HTTPException(
        status_code=403,
        detail="cloud import is no longer available in the UI; use rclone.conf directly",
    )


@app.post("/api/clouds/test", dependencies=[Depends(require_write_access)])
def test_cloud_settings() -> dict[str, Any]:
    raise HTTPException(
        status_code=403,
        detail="cloud testing from the UI is disabled; validate remotes with rclone directly",
    )


@app.post("/api/gotify/test", dependencies=[Depends(require_write_access)])
def test_gotify_settings(payload: GotifyPayload) -> dict[str, Any]:
    gotify_settings = GotifySettings(**payload.model_dump()).normalized()
    if not gotify_settings.is_configured():
        raise HTTPException(status_code=400, detail="gotify is not fully configured")
    sent = gotify.send(
        gotify_settings,
        title="Rclone taskboard Test",
        message=(
            f"Тестовое уведомление из {settings.app_name}\n"
            f"time={datetime.now(timezone.utc).isoformat()}"
        ),
        priority=gotify_settings.default_priority,
    )
    if not sent:
        raise HTTPException(status_code=502, detail="failed to send gotify notification")
    return {"sent": True}


@app.put("/api/backups", dependencies=[Depends(require_write_access)])
def update_backups(payload: BackupCatalogPayload) -> dict[str, Any]:
    backup_jobs: list[JobDefinition] = []
    seen_keys: set[str] = set()
    live_clouds = {cloud.key: cloud for cloud in _refresh_catalog_clouds_from_rclone()}
    queue_keys = set(catalog.queues.queue_keys())

    for item in payload.jobs:
        key = item.key.strip()
        if not key:
            raise HTTPException(status_code=400, detail="backup key is required")
        if key in seen_keys:
            raise HTTPException(status_code=400, detail=f"duplicate backup key '{key}'")
        if item.profile not in queue_keys:
            raise HTTPException(status_code=400, detail=f"unknown queue '{item.profile}'")
        if item.retention.enabled and not (item.retention.min_age or "").strip():
            raise HTTPException(status_code=400, detail=f"backup '{key}' retention requires min_age")
        seen_keys.add(key)
        cloud = live_clouds.get(item.cloud_key) if item.cloud_key else None
        destination_path = _compose_cloud_destination(cloud, item.destination_subpath) or item.destination_path
        backup_jobs.append(
            JobDefinition(
                key=key,
                order=item.order,
                description=item.description or item.title or key,
                title=item.title,
                timeout_seconds=item.timeout_seconds,
                enabled=item.enabled,
                continue_on_error=item.continue_on_error,
                kind="backup",
                profile=item.profile,
                schedule=ScheduleDefinition(**item.schedule.model_dump()),
                source_path=item.source_path,
                cloud_key=item.cloud_key,
                destination_subpath=item.destination_subpath,
                destination_path=destination_path,
                transfer_mode=item.transfer_mode,
                options=BackupOptions(**item.options.model_dump()),
                retention=RetentionSettings(**item.retention.model_dump()),
                notifications=JobNotificationSettings(**item.notifications.model_dump()),
                watcher_enabled=item.watcher_enabled,
            ).validate()
        )

    with catalog_lock:
        command_jobs = [job for job in catalog.raw_jobs() if job.kind == "command"]
        merged_jobs = sorted(command_jobs + backup_jobs, key=lambda job: (job.order, job.key))
        updated_catalog = JobCatalog(
            jobs=merged_jobs,
            profiles=build_profiles(merged_jobs, queue_keys=catalog.queues.queue_keys()),
            gotify=catalog.gotify,
            queues=catalog.queues,
            bandwidth=catalog.bandwidth,
            logging=catalog.logging,
            watcher=catalog.watcher,
            clouds=catalog.raw_clouds(),
        )
        save_catalog(settings.jobs_file, updated_catalog)
        catalog.replace(
            updated_catalog.raw_jobs(),
            updated_catalog.profiles,
            gotify=updated_catalog.gotify,
            queues=updated_catalog.queues,
            bandwidth=updated_catalog.bandwidth,
            logging=updated_catalog.logging,
            watcher=updated_catalog.watcher,
            clouds=updated_catalog.raw_clouds(),
        )
        event_watcher.sync_from_catalog()

    return {
        "saved": True,
        "backup_jobs": catalog.list_backup_jobs(),
        "profiles": catalog.profiles,
        "gotify": catalog.gotify.to_dict(),
        "queues": catalog.queues.to_dict(),
        "bandwidth": catalog.bandwidth.to_dict(),
        "logging": catalog.logging.to_dict(),
        "watcher": catalog.watcher.to_dict(),
        "clouds": catalog.list_clouds(),
    }


@app.put("/api/jobs", dependencies=[Depends(require_write_access)])
def update_jobs(payload: JobCatalogPayload) -> dict[str, Any]:
    jobs_to_save: list[JobDefinition] = []
    seen_keys: set[str] = set()
    live_clouds = {cloud.key: cloud for cloud in _refresh_catalog_clouds_from_rclone()}
    queue_keys = set(catalog.queues.queue_keys())

    for item in payload.jobs:
        key = item.key.strip()
        if not key:
            raise HTTPException(status_code=400, detail="job key is required")
        if key in seen_keys:
            raise HTTPException(status_code=400, detail=f"duplicate job key '{key}'")
        if item.profile not in queue_keys:
            raise HTTPException(status_code=400, detail=f"unknown queue '{item.profile}'")
        seen_keys.add(key)
        if item.kind == "backup" and item.retention.enabled and not (item.retention.min_age or "").strip():
            raise HTTPException(status_code=400, detail=f"backup '{key}' retention requires min_age")

        common_kwargs = dict(
            key=key,
            order=item.order,
            description=item.description or item.title or key,
            title=item.title,
            timeout_seconds=item.timeout_seconds,
            enabled=item.enabled,
            continue_on_error=item.continue_on_error,
            kind=item.kind,
            profile=item.profile,
            schedule=ScheduleDefinition(**item.schedule.model_dump()),
            notifications=JobNotificationSettings(**item.notifications.model_dump()),
        )
        if item.kind == "command":
            jobs_to_save.append(
                JobDefinition(
                    **common_kwargs,
                    command=[part for part in item.command if str(part).strip()],
                    options=BackupOptions(force_rclone_log=item.options.force_rclone_log),
                ).validate()
            )
        else:
            cloud = live_clouds.get(item.cloud_key) if item.cloud_key else None
            destination_path = _compose_cloud_destination(cloud, item.destination_subpath) or item.destination_path
            jobs_to_save.append(
                JobDefinition(
                    **common_kwargs,
                    source_path=item.source_path,
                    cloud_key=item.cloud_key,
                    destination_subpath=item.destination_subpath,
                    destination_path=destination_path,
                    transfer_mode=item.transfer_mode,
                    options=BackupOptions(**item.options.model_dump()),
                    retention=RetentionSettings(**item.retention.model_dump()),
                    watcher_enabled=item.watcher_enabled,
                ).validate()
            )

    with catalog_lock:
        updated_catalog = JobCatalog(
            jobs=sorted(jobs_to_save, key=lambda job: (job.order, job.key)),
            profiles=build_profiles(jobs_to_save, queue_keys=catalog.queues.queue_keys()),
            gotify=catalog.gotify,
            queues=catalog.queues,
            bandwidth=catalog.bandwidth,
            logging=catalog.logging,
            watcher=catalog.watcher,
            clouds=catalog.raw_clouds(),
        )
        save_catalog(settings.jobs_file, updated_catalog)
        catalog.replace(
            updated_catalog.raw_jobs(),
            updated_catalog.profiles,
            gotify=updated_catalog.gotify,
            queues=updated_catalog.queues,
            bandwidth=updated_catalog.bandwidth,
            logging=updated_catalog.logging,
            watcher=updated_catalog.watcher,
            clouds=updated_catalog.raw_clouds(),
        )
        event_watcher.sync_from_catalog()

    return {
        "saved": True,
        "jobs": catalog.list_jobs(),
        "backup_jobs": catalog.list_backup_jobs(),
        "command_jobs": catalog.list_command_jobs(),
        "profiles": catalog.profiles,
        "gotify": catalog.gotify.to_dict(),
        "queues": catalog.queues.to_dict(),
        "bandwidth": catalog.bandwidth.to_dict(),
        "logging": catalog.logging.to_dict(),
        "watcher": catalog.watcher.to_dict(),
        "clouds": catalog.list_clouds(),
    }


@app.get("/api/fs/browse")
def browse_directories(path: str | None = None, include_files: bool = False) -> dict[str, Any]:
    if not path:
        roots = []
        for root in FS_ROOTS:
            root_path = Path(root)
            if root_path.exists() and root_path.is_dir():
                roots.append(
                    {
                        "name": root_path.name or root_path.as_posix(),
                        "path": root_path.as_posix(),
                    }
                )
        return {"path": None, "parent": None, "directories": roots}

    target = Path(path).expanduser()
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")

    directories: list[dict[str, str]] = []
    files: list[dict[str, str]] = []
    try:
        for child in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir():
                directories.append({"name": child.name, "path": child.as_posix()})
            elif include_files and child.is_file():
                files.append({"name": child.name, "path": child.as_posix()})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"permission denied: {exc}") from exc

    parent = target.parent.as_posix() if target.parent != target else None
    return {
        "path": target.as_posix(),
        "parent": parent,
        "directories": directories,
        "files": files,
    }


@app.get("/api/runs")
def list_runs(limit: int = Query(default=50, ge=1, le=999)) -> dict[str, Any]:
    return {"runs": storage.list_runs(limit=limit)}


@app.delete("/api/runs", dependencies=[Depends(require_write_access)])
def clear_run_history() -> dict[str, Any]:
    return {
        "cleared": True,
        **storage.clear_run_history(),
    }


@app.get("/api/runs/{run_id}")
def run_details(run_id: int) -> dict[str, Any]:
    run = storage.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "run": run,
        "steps": storage.list_run_steps(run_id),
    }


@app.post("/api/runs", dependencies=[Depends(require_write_access)])
def create_run(payload: RunCreateRequest) -> dict[str, Any]:
    if payload.profile not in catalog.profiles:
        raise HTTPException(
            status_code=400,
            detail=f"unknown profile '{payload.profile}'",
        )
    try:
        run_id = orchestrator.enqueue_run(
            profile=payload.profile,
            trigger_type="manual",
            source=payload.source,
            requested_by=payload.requested_by,
            metadata=payload.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"accepted": True, "run_id": run_id}


@app.post("/api/runs/job/{job_key}", dependencies=[Depends(require_write_access)])
def create_job_run(job_key: str) -> dict[str, Any]:
    try:
        run_id = orchestrator.enqueue_job(
            job_key=job_key,
            trigger_type="manual",
            source="dashboard",
            requested_by="dashboard",
            metadata={"job_key": job_key},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"accepted": True, "run_id": run_id}


@app.post("/api/run-steps/{step_id}/control", dependencies=[Depends(require_write_access)])
def control_run_step(step_id: int, payload: RunStepControlPayload) -> dict[str, Any]:
    try:
        return orchestrator.control_run_step(step_id, payload.action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/triggers/event", dependencies=[Depends(require_write_access)])
def trigger_event(payload: EventTriggerRequest) -> dict[str, Any]:
    data = payload.model_dump()
    return orchestrator.enqueue_event(data)
