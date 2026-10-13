from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
from typing import Any, Callable

WATCHDOG_IMPORT_ERROR: str | None = None
try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileMovedEvent
    from watchdog.observers import Observer
except ModuleNotFoundError as exc:  # pragma: no cover - fallback for stale local envs
    WATCHDOG_IMPORT_ERROR = str(exc)
    Observer = None  # type: ignore[assignment]

    class FileSystemEvent:  # type: ignore[no-redef]
        event_type = ""
        src_path = ""
        is_directory = False

    class FileMovedEvent(FileSystemEvent):  # type: ignore[no-redef]
        dest_path = ""

    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

from .domain import JobCatalog, path_is_excluded_from_backup, path_is_within


logger = logging.getLogger(__name__)
SUPPORTED_EVENT_TYPES = {"created", "modified", "deleted", "moved", "closed"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _CatalogWatchHandler(FileSystemEventHandler):
    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit = emit

    def on_any_event(self, event: FileSystemEvent) -> None:
        event_type = str(getattr(event, "event_type", "") or "").strip().lower()
        if event_type not in SUPPORTED_EVENT_TYPES:
            return
        payload: dict[str, Any] = {
            "event_type": "filesystem",
            "path": str(getattr(event, "src_path", "") or "").strip(),
            "details": {
                "event": event_type,
                "is_directory": bool(getattr(event, "is_directory", False)),
            },
        }
        if isinstance(event, FileMovedEvent):
            payload["details"]["src_path"] = str(event.src_path)
            payload["details"]["dest_path"] = str(event.dest_path)
            if event.dest_path:
                payload["path"] = str(event.dest_path)
        self._emit(payload)


class FilesystemWatcher:
    def __init__(
        self,
        *,
        catalog: JobCatalog,
        on_event: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> None:
        self.catalog = catalog
        self.on_event = on_event
        self._lock = threading.RLock()
        self._started = False
        self._observer: Observer | None = None
        self._entries: list[dict[str, Any]] = []
        self._last_event_seen_at: str | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._apply_catalog_locked()

    def stop(self) -> None:
        with self._lock:
            self._started = False
            self._stop_observer_locked()

    def sync_from_catalog(self) -> None:
        with self._lock:
            self._apply_catalog_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            entries = [dict(item) for item in self._entries]
            configured_paths = sorted({item["path"] for item in entries})
            active_paths = sorted({item["path"] for item in entries if item["active"]})
            observer_running = bool(self._observer and self._observer.is_alive())
            return {
                "enabled": self.catalog.watcher.enabled,
                "debounce_seconds": self.catalog.watcher.debounce_seconds,
                "component_running": self._started,
                "observer_running": observer_running,
                "configured_jobs": len(entries),
                "active_jobs": sum(1 for item in entries if item["active"]),
                "configured_paths": configured_paths,
                "active_paths": active_paths,
                "jobs": entries,
                "last_event_seen_at": self._last_event_seen_at,
                "last_error": self._last_error,
            }

    def _collect_watch_entries(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        watcher_enabled = self.catalog.watcher.enabled
        for job in self.catalog.raw_jobs():
            if job.kind != "backup" or job.transfer_direction == "download" or not job.enabled or not job.watcher_enabled or not job.source_path:
                continue
            source_path = job.source_path.rstrip("/") or job.source_path
            exists = False
            is_dir = False
            try:
                path_obj = Path(source_path).expanduser()
                exists = path_obj.exists()
                is_dir = path_obj.is_dir()
            except OSError:
                exists = False
                is_dir = False
            items.append(
                {
                    "job_key": job.key,
                    "title": job.title or job.description,
                    "profile": job.profile,
                    "path": source_path,
                    "exists": exists,
                    "is_directory": is_dir,
                    "active": watcher_enabled and exists and is_dir,
                }
            )
        items.sort(key=lambda item: (item["path"], item["job_key"]))
        return items

    def _apply_catalog_locked(self) -> None:
        self._entries = self._collect_watch_entries()
        self._stop_observer_locked()
        if not self._started or not self.catalog.watcher.enabled:
            return
        if WATCHDOG_IMPORT_ERROR or Observer is None:
            self._last_error = f"watchdog is unavailable: {WATCHDOG_IMPORT_ERROR or 'unknown import error'}"
            return
        active_paths = sorted({item["path"] for item in self._entries if item["active"]})
        if not active_paths:
            return

        handler = _CatalogWatchHandler(self._handle_event)
        observer = Observer()
        try:
            for path in active_paths:
                observer.schedule(handler, path, recursive=True)
            observer.start()
            self._observer = observer
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("failed to start filesystem watcher")
            try:
                observer.stop()
                observer.join(timeout=3)
            except Exception:
                logger.exception("failed to stop broken filesystem watcher")
            self._observer = None

    def _stop_observer_locked(self) -> None:
        observer = self._observer
        self._observer = None
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=5)
        except Exception:
            logger.exception("failed to stop filesystem watcher")

    def _handle_event(self, payload: dict[str, Any]) -> None:
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        candidate_paths = [
            str(payload.get("path") or "").strip(),
            str(details.get("src_path") or "").strip(),
            str(details.get("dest_path") or "").strip(),
        ]
        if not self._matches_active_non_excluded_entry(candidate_paths):
            return
        self._last_event_seen_at = utc_now_iso()
        try:
            self.on_event(payload)
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("filesystem watcher callback failed")

    def _matches_active_non_excluded_entry(self, candidate_paths: list[str]) -> bool:
        for item in self._entries:
            if not item["active"]:
                continue
            job = self.catalog.get_job(str(item.get("job_key") or ""))
            if not job:
                continue
            for candidate in candidate_paths:
                if not candidate or not path_is_within(item["path"], candidate):
                    continue
                if path_is_excluded_from_backup(
                    source_path=job.source_path,
                    target_path=candidate,
                    options=job.options,
                ):
                    continue
                return True
        return False
