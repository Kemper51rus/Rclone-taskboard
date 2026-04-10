from __future__ import annotations

import configparser
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
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
)
from .gotify import GotifyClient
from .jobs_loader import build_profiles, load_catalog, save_catalog
from .orchestrator import Orchestrator
from .runner import CommandRunner
from .storage import Storage


settings: Settings = load_settings()
catalog = load_catalog(
    settings.jobs_file,
    standard_interval_minutes=settings.standard_interval_minutes,
    heavy_hour=settings.heavy_hour,
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
DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
FS_ROOTS = ["/media", "/srv", "/home", "/root", "/mnt", "/tmp"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    storage.initialize()
    storage.recover_incomplete_runs()
    orchestrator.start()
    try:
        yield
    finally:
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


class BackupOptionsPayload(BaseModel):
    max_age: str | None = None
    min_age: str | None = None
    exclude: list[str] = Field(default_factory=list)
    extra_args: list[str] = Field(default_factory=list)


class JobNotificationPayload(BaseModel):
    on_success: bool = False
    on_failure: bool = True
    priority: int | None = None
    custom_title: str | None = None


class RetentionPayload(BaseModel):
    enabled: bool = False
    min_age: str | None = None
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
            clouds=refreshed_clouds,
        )
        return catalog.raw_clouds()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "utc_time": datetime.now(timezone.utc).isoformat(),
        "app": settings.app_name,
    }


@app.get("/api/state")
def state() -> dict[str, Any]:
    snapshot = orchestrator.snapshot()
    snapshot["token_required"] = bool(settings.api_token)
    snapshot["latest_runs"] = storage.list_runs(limit=15)
    snapshot["backup_jobs"] = catalog.list_backup_jobs()
    return snapshot


@app.get("/api/jobs")
def jobs() -> dict[str, Any]:
    clouds = _refresh_catalog_clouds_from_rclone()
    return {
        "profiles": catalog.profiles,
        "gotify": catalog.gotify.to_dict(),
        "queues": catalog.queues.to_dict(),
        "bandwidth": catalog.bandwidth.to_dict(),
        "logging": catalog.logging.to_dict(),
        "clouds": [cloud.to_dict() for cloud in clouds],
        "jobs": catalog.list_jobs(),
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


@app.get("/api/logging/rclone-tail")
def get_rclone_log_tail(lines: int = Query(default=100, ge=1, le=2000)) -> dict[str, Any]:
    logs_dir = settings.app_root / "data" / "rclone-logs"
    if not logs_dir.exists() or not logs_dir.is_dir():
        return {"path": None, "lines": lines, "content": "", "available": False}

    candidates = [path for path in logs_dir.glob("*.log") if path.is_file()]
    if not candidates:
        return {"path": None, "lines": lines, "content": "", "available": False}

    latest_path = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        content = "\n".join(
            latest_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to read log: {exc}") from exc

    return {
        "path": latest_path.relative_to(settings.app_root).as_posix(),
        "lines": lines,
        "content": content,
        "available": True,
    }


@app.delete("/api/logging/rclone-log", dependencies=[Depends(require_write_access)])
def clear_rclone_logs() -> dict[str, Any]:
    logs_dir = settings.app_root / "data" / "rclone-logs"
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


@app.get("/api/clouds")
def get_cloud_settings() -> dict[str, Any]:
    clouds = _refresh_catalog_clouds_from_rclone()
    return {"clouds": [cloud.to_dict() for cloud in clouds]}


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
    queue_keys = set(queues.queue_keys())
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
            clouds=updated_catalog.raw_clouds(),
        )
    return {"saved": True, "logging": catalog.logging.to_dict()}


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
        title="Rclone Hybrid Test",
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
            clouds=updated_catalog.raw_clouds(),
        )

    return {
        "saved": True,
        "backup_jobs": catalog.list_backup_jobs(),
        "profiles": catalog.profiles,
        "gotify": catalog.gotify.to_dict(),
        "queues": catalog.queues.to_dict(),
        "bandwidth": catalog.bandwidth.to_dict(),
        "logging": catalog.logging.to_dict(),
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
            clouds=updated_catalog.raw_clouds(),
        )

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
        "clouds": catalog.list_clouds(),
    }


@app.get("/api/fs/browse")
def browse_directories(path: str | None = None) -> dict[str, Any]:
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
    try:
        for child in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir():
                directories.append({"name": child.name, "path": child.as_posix()})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"permission denied: {exc}") from exc

    parent = target.parent.as_posix() if target.parent != target else None
    return {
        "path": target.as_posix(),
        "parent": parent,
        "directories": directories,
    }


@app.get("/api/runs")
def list_runs(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
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
