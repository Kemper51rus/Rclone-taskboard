from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import queue
import re
import threading
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .domain import (
    JobCatalog,
    JobDefinition,
    RunStepDefinition,
    apply_rclone_bwlimit,
    effective_bwlimit,
    path_is_within,
)
from .gotify import GotifyClient
from .runner import CommandRunner
from .storage import Storage


logger = logging.getLogger(__name__)
COPY_LAST_STARTED_AT_STATE_KEY = "copy_last_started_at"
AUTO_LOG_ENABLED_STATE_PREFIX = "job_auto_rclone_log_enabled:"
AUTO_LOG_FAILURE_STREAK_STATE_PREFIX = "job_auto_rclone_log_failure_streak:"
AUTO_LOG_SUCCESS_STREAK_STATE_PREFIX = "job_auto_rclone_log_success_streak:"
DATA_SIZE_RE = re.compile(r"^([0-9]+(?:[.,][0-9]+)?)\s*([KMGTPE]?i?B)$", re.IGNORECASE)
RCLONE_LOG_STATS_RE = re.compile(
    r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} INFO\s+:\s+(.+?) / (.+?),\s+([0-9-]+)%,\s+([^,]+),\s+ETA\s+(.+?)(?:\s+\(xfr#.*\))?$"
)
RCLONE_LOG_ZERO_RE = re.compile(
    r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} INFO\s+:\s+(.+?) / (.+?),\s+-\s*,\s+([^,]+),\s+ETA\s+(.+)$"
)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        catalog: JobCatalog,
        runner: CommandRunner,
        gotify: GotifyClient,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.catalog = catalog
        self.runner = runner
        self.gotify = gotify

        self._stop_event = threading.Event()
        self._queue_lock = threading.RLock()
        self._run_queues: dict[str, queue.Queue[int | None]] = {}
        self._worker_threads: dict[str, list[threading.Thread]] = {}
        self._delayed_runs_by_queue: dict[str, set[int]] = {}

        self._scheduler_thread: threading.Thread | None = None
        self._copy_start_gate_lock = threading.Lock()
        self._copy_starts_allowed_after: datetime | None = None

    def start(self) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return

        self._stop_event.clear()
        self._copy_starts_allowed_after = datetime.now(timezone.utc) + timedelta(
            seconds=self.settings.copy_startup_delay_seconds
        )
        self.sync_workers_from_catalog()

        if self.settings.enable_scheduler:
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="hybrid-scheduler",
                daemon=True,
            )
            self._scheduler_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._queue_lock:
            threads = [thread for items in self._worker_threads.values() for thread in items]
            for queue_name, workers in self._worker_threads.items():
                run_queue = self._run_queues.get(queue_name)
                if run_queue is None:
                    continue
                for _ in workers:
                    run_queue.put(None)
        for thread in threads:
            thread.join(timeout=5)
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)

    def sync_workers_from_catalog(self) -> None:
        desired = {
            definition.key: definition.workers
            for definition in self.catalog.raw_queue_definitions()
            if definition.enabled
        }
        with self._queue_lock:
            for queue_name in desired:
                self._run_queues.setdefault(queue_name, queue.Queue())
                self._worker_threads.setdefault(queue_name, [])
                self._delayed_runs_by_queue.setdefault(queue_name, set())

            for queue_name, worker_count in desired.items():
                current = self._worker_threads.get(queue_name, [])
                while len(current) < worker_count:
                    worker_index = len(current) + 1
                    thread = threading.Thread(
                        target=self._worker_loop,
                        args=(queue_name, self._run_queues[queue_name]),
                        name=f"hybrid-worker-{queue_name}-{worker_index}",
                        daemon=True,
                    )
                    current.append(thread)
                    thread.start()

            for queue_name, current in list(self._worker_threads.items()):
                target = desired.get(queue_name, 0)
                run_queue = self._run_queues.get(queue_name)
                while len(current) > target and run_queue is not None:
                    run_queue.put(None)
                    current.pop()

    def enqueue_run(
        self,
        profile: str,
        trigger_type: str,
        source: str,
        requested_by: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        steps = self.catalog.steps_for_profile(profile)
        if not steps:
            raise ValueError(f"profile '{profile}' has no enabled steps")
        queue_profile = profile if profile != "all" else steps[0].profile

        return self._enqueue_steps(
            queue_profile=queue_profile,
            run_profile=profile,
            steps=steps,
            trigger_type=trigger_type,
            source=source,
            requested_by=requested_by,
            metadata=metadata,
        )

    def enqueue_job(
        self,
        job_key: str,
        trigger_type: str,
        source: str,
        requested_by: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        job = self.catalog.get_job(job_key)
        if not job:
            raise ValueError(f"unknown job '{job_key}'")
        if not job.enabled:
            raise ValueError(f"job '{job_key}' is disabled")
        return self._enqueue_steps(
            queue_profile=job.profile,
            run_profile=job.profile,
            steps=[job],
            trigger_type=trigger_type,
            source=source,
            requested_by=requested_by,
            metadata={**(metadata or {}), "job_key": job_key, "scheduled": True},
        )

    def _enqueue_steps(
        self,
        queue_profile: str,
        run_profile: str,
        steps: list[JobDefinition],
        trigger_type: str,
        source: str,
        requested_by: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        if not steps:
            raise ValueError("no steps to enqueue")
        expanded_steps = self._expand_steps(steps)
        if not expanded_steps:
            raise ValueError("no runnable steps to enqueue")

        run_id = self.storage.create_run(
            profile=run_profile,
            trigger_type=trigger_type,
            source=source,
            requested_by=requested_by,
            metadata=metadata or {},
        )
        self.storage.insert_run_steps(run_id, expanded_steps)

        target_queue = self._queue_for_profile(queue_profile)
        target_queue.put(run_id)
        return run_id

    def enqueue_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.storage.append_event("filesystem", payload)
        event_path = str(payload.get("path") or "").strip() or None
        matched_jobs = self._matching_watcher_jobs(payload)

        if not self.catalog.watcher.enabled:
            return {
                "accepted": False,
                "reason": "watcher_disabled",
                "matched_jobs": [],
                "job_results": [],
            }

        if not matched_jobs:
            return {
                "accepted": False,
                "reason": "no_matching_jobs",
                "matched_jobs": [],
                "job_results": [],
            }

        now = datetime.now(timezone.utc)
        debounce_seconds = self.catalog.watcher.debounce_seconds
        run_ids: list[int] = []
        job_results: list[dict[str, Any]] = []

        for job in matched_jobs:
            state_key = f"watcher_last_enqueued_at:{job.key}"
            last_ts_raw = self.storage.get_state(state_key)
            if last_ts_raw:
                try:
                    last_ts = datetime.fromisoformat(last_ts_raw)
                    elapsed = (now - last_ts).total_seconds()
                    if elapsed < debounce_seconds:
                        job_results.append(
                            {
                                "job_key": job.key,
                                "accepted": False,
                                "reason": "debounced",
                                "retry_after_seconds": int(debounce_seconds - elapsed),
                            }
                        )
                        continue
                except ValueError:
                    pass

            if self._event_enqueue_blocked(job.profile):
                job_results.append(
                    {
                        "job_key": job.key,
                        "accepted": False,
                        "reason": "queue_busy",
                        "profile": job.profile,
                    }
                )
                continue

            run_id = self.enqueue_job(
                job_key=job.key,
                trigger_type="event",
                source="watcher",
                requested_by="watcher",
                metadata={
                    **payload,
                    "matched_job_key": job.key,
                    "watch_path": event_path,
                },
            )
            self.storage.set_state(state_key, now.isoformat())
            self.storage.set_state("event_last_enqueued_at", now.isoformat())
            run_ids.append(run_id)
            job_results.append(
                {
                    "job_key": job.key,
                    "accepted": True,
                    "profile": job.profile,
                    "run_id": run_id,
                }
            )

        return {
            "accepted": bool(run_ids),
            "run_ids": run_ids,
            "matched_jobs": [job.key for job in matched_jobs],
            "job_results": job_results,
            "reason": None if run_ids else "no_jobs_enqueued",
        }

    def control_run_step(self, step_id: int, action: str) -> dict[str, Any]:
        step = self.storage.get_run_step(step_id)
        if not step:
            raise ValueError("run step not found")
        step_status = str(step.get("status") or "")
        if action == "stop" and step_status == "queued":
            run_id = int(step["run_id"])
            changed = self.storage.stop_queued_run(run_id, summary="stopped from dashboard before start")
            self._clear_run_delayed_any(run_id)
            if not changed:
                raise ValueError("step is no longer queued")
            return {
                "ok": True,
                "step_id": step_id,
                "action": action,
            }
        if step_status != "running":
            raise ValueError("only running steps can be paused or resumed")
        if action == "pause":
            changed = self.runner.pause(step_id)
        elif action == "resume":
            changed = self.runner.resume(step_id)
        elif action == "stop":
            changed = self.runner.stop(step_id)
        else:
            raise ValueError(f"unsupported action '{action}'")
        if not changed:
            raise ValueError("step is no longer active")
        return {
            "ok": True,
            "step_id": step_id,
            "action": action,
        }

    def snapshot(self) -> dict[str, Any]:
        queue_statuses = self._queue_status_snapshot()
        queue_status_by_key = {item["key"]: item for item in queue_statuses}
        return {
            "queue_statuses": queue_statuses,
            "standard_queue_size": queue_status_by_key.get("standard", {}).get("queued_runs", 0),
            "heavy_queue_size": queue_status_by_key.get("heavy", {}).get("queued_runs", 0),
            "standard_worker_alive": queue_status_by_key.get("standard", {}).get("alive_workers", 0) > 0,
            "heavy_worker_alive": queue_status_by_key.get("heavy", {}).get("alive_workers", 0) > 0,
            "scheduler_alive": bool(
                self._scheduler_thread and self._scheduler_thread.is_alive()
            ),
            "open_runs_total": self.storage.open_run_count(),
            "open_runs_standard": self.storage.open_run_count("standard"),
            "open_runs_heavy": self.storage.open_run_count("heavy"),
            "last_standard_tick": self.storage.get_state("scheduler_last_standard_tick"),
            "last_heavy_day": self.storage.get_state("scheduler_last_heavy_day"),
            "last_event_enqueued_at": self.storage.get_state("event_last_enqueued_at"),
            "last_copy_started_at": self.storage.get_state(COPY_LAST_STARTED_AT_STATE_KEY),
            "copy_starts_allowed_after": (
                self._copy_starts_allowed_after.isoformat()
                if self._copy_starts_allowed_after is not None
                else None
            ),
            "next_copy_start_at": self._next_copy_start_at(),
            "copy_progress": self._copy_progress_snapshot(),
            "total_copy_speed_bytes_per_second": self._total_copy_speed_bytes_per_second(),
            "active_operations": self._active_operations_snapshot(),
        }

    def _queue_for_profile(self, profile: str) -> queue.Queue[int | None]:
        with self._queue_lock:
            run_queue = self._run_queues.get(profile)
            if run_queue is None:
                raise ValueError(f"queue '{profile}' is not configured")
            return run_queue

    def _queue_status_snapshot(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        with self._queue_lock:
            for definition in self.catalog.raw_queue_definitions():
                workers = self._worker_threads.get(definition.key, [])
                run_queue = self._run_queues.get(definition.key)
                items.append(
                    {
                        "key": definition.key,
                        "title": definition.title,
                        "workers": definition.workers,
                        "alive_workers": sum(1 for thread in workers if thread.is_alive()),
                        "queued_runs": (run_queue.qsize() if run_queue else 0)
                        + len(self._delayed_runs_by_queue.get(definition.key, set())),
                        "open_runs": self.storage.open_run_count(definition.key),
                        "bandwidth_limit": definition.bandwidth_limit,
                        "enabled": definition.enabled,
                    }
                )
        return items

    def _queue_busy(self, profile: str) -> bool:
        definition = self.catalog.get_queue_definition(profile)
        capacity = max(1, definition.workers if definition else 1)
        if self.storage.open_run_count(profile) >= capacity:
            return True
        if not self.catalog.queues.allow_parallel_profiles and self.storage.open_run_count() > 0:
            return True
        return False

    def _scheduler_enqueue_blocked(self, profile: str) -> bool:
        if self.catalog.queues.allow_scheduler_queueing:
            return False
        return self._queue_busy(profile)

    def _event_enqueue_blocked(self, profile: str) -> bool:
        if self.catalog.queues.allow_event_queueing:
            return False
        return self._queue_busy(profile)

    def _matching_watcher_jobs(self, payload: dict[str, Any]) -> list[JobDefinition]:
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        candidate_paths = [
            str(payload.get("path") or "").strip() or None,
            str(details.get("src_path") or "").strip() or None,
            str(details.get("dest_path") or "").strip() or None,
        ]
        matched: list[JobDefinition] = []
        for job in self.catalog.raw_jobs():
            if job.kind != "backup" or not job.enabled or not job.watcher_enabled or not job.source_path:
                continue
            if any(path_is_within(job.source_path, candidate) for candidate in candidate_paths if candidate):
                matched.append(job)
        matched.sort(key=lambda item: (item.order, item.key))
        return matched

    def _worker_loop(self, queue_name: str, run_queue: queue.Queue[int | None]) -> None:
        while not self._stop_event.is_set():
            try:
                item = run_queue.get(timeout=1)
            except queue.Empty:
                continue

            if item is None:
                break

            try:
                self._process_run(item, queue_name)
            except Exception:
                logger.exception("run %s failed in %s queue", item, queue_name)

    def _process_run(self, run_id: int, queue_name: str) -> None:
        run = self.storage.get_run(run_id)
        if not run:
            return
        if str(run.get("status") or "") not in {"queued", "running"}:
            self._clear_run_delayed_any(run_id)
            return

        steps = self.storage.list_run_steps(run_id)
        total_steps = len(steps)
        completed_steps = 0
        error_count = 0
        failed_jobs: list[str] = []
        run_started = False

        for step in steps:
            step_id = int(step["id"])
            if self._step_needs_copy_gate(step):
                if not run_started:
                    self._mark_run_delayed(queue_name, run_id)
                    try:
                        if not self._wait_for_copy_start_slot(run_id=run_id, step_id=step_id, step=step):
                            return
                    finally:
                        self._clear_run_delayed(queue_name, run_id)
                else:
                    if not self._wait_for_copy_start_slot(run_id=run_id, step_id=step_id, step=step):
                        return
            if not run_started:
                self.storage.mark_run_running(run_id)
                run_started = True
            self.storage.mark_step_running(step_id)
            log_mode = self._step_rclone_log_mode(str(step.get("job_key") or ""))
            self.storage.set_step_log_mode(step_id, log_mode)

            command = self._bind_step_rclone_log(
                command=list(step.get("command", [])),
                run_id=run_id,
                step_id=step_id,
                job_key=str(step.get("job_key") or ""),
                log_mode=log_mode,
            )
            timeout_seconds = int(step.get("timeout_seconds") or self.settings.default_timeout_seconds)
            result = self.runner.run(
                command=command,
                timeout_seconds=timeout_seconds,
                on_progress=lambda progress, current_step_id=step_id: self.storage.update_step_progress(
                    current_step_id,
                    progress,
                ),
                control_id=step_id,
            )

            self.storage.mark_step_finished(
                step_id=step_id,
                status=result.status,
                duration_seconds=result.duration_seconds,
                exit_code=result.exit_code,
                stdout_tail=result.stdout_tail,
                stderr_tail=result.stderr_tail,
            )
            self._update_job_auto_rclone_log_state(step=step, status=result.status)
            self._notify_for_step(run=run, step=step, result=result)

            completed_steps += 1
            if result.status != "succeeded":
                error_count += 1
                failed_jobs.append(str(step.get("job_key", "unknown")))
                if not bool(step.get("continue_on_error", 0)):
                    self.storage.skip_pending_steps(
                        run_id=run_id,
                        after_step_order=int(step["step_order"]),
                    )
                    break

        status = "succeeded" if error_count == 0 else "failed"
        summary = f"completed={completed_steps}/{total_steps}; errors={error_count}"
        if failed_jobs:
            summary = f"{summary}; failed_jobs={','.join(failed_jobs)}"
        self.storage.mark_run_finished(
            run_id=run_id,
            status=status,
            summary=summary,
            error_count=error_count,
        )

    def _notify_for_step(self, run: dict[str, Any], step: dict[str, Any], result: Any) -> None:
        if step.get("step_kind") != "job":
            return
        job = self.catalog.get_job(str(step.get("job_key", "")))
        if not job:
            return
        notifications = job.notifications.normalized()
        if result.status == "succeeded" and not notifications.on_success:
            return
        if result.status != "succeeded" and not notifications.on_failure:
            return

        priority = notifications.priority or self.catalog.gotify.default_priority
        title_prefix = notifications.custom_title or job.title or job.description or job.key
        title = f"{title_prefix}: {'OK' if result.status == 'succeeded' else 'FAILED'}"
        message = "\n".join(
            [
                f"job={job.key}",
                f"profile={run.get('profile', job.profile)}",
                f"status={result.status}",
                f"trigger={run.get('trigger_type', 'manual')}",
                f"requested_by={run.get('requested_by', 'dashboard')}",
                f"duration={result.duration_seconds:.2f}s",
                f"exit_code={result.exit_code if result.exit_code is not None else 'n/a'}",
            ]
        )
        if result.stderr_tail:
            message = f"{message}\n\nstderr_tail:\n{result.stderr_tail[-1200:]}"
        self.gotify.send(
            self.catalog.gotify,
            title=title,
            message=message,
            priority=priority,
        )

    def _copy_progress_snapshot(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for step in self.storage.list_open_run_steps():
            if step.get("step_kind") != "job":
                continue
            job = self.catalog.get_job(str(step.get("job_key", "")))
            if not job or job.kind != "backup":
                continue
            progress = step.get("progress") or {}
            if step.get("log_mode") and not progress and step.get("status") == "running":
                progress = self._read_progress_from_rclone_log(
                    started_at_raw=step.get("started_at"),
                    log_path=self._step_rclone_log_path(
                        run_id=int(step["run_id"]),
                        step_id=int(step["id"]),
                    ),
                )
            effective_status = "paused" if self.runner.is_paused(int(step["id"])) else step["status"]
            items.append(
                {
                    "step_id": step["id"],
                    "run_id": step["run_id"],
                    "step_order": step["step_order"],
                    "job_key": job.key,
                    "title": job.title or job.description or job.key,
                    "status": effective_status,
                    "profile": step.get("run_profile"),
                    "trigger_type": step.get("run_trigger_type"),
                    "requested_at": step.get("run_requested_at"),
                    "started_at": step.get("started_at"),
                    "progress_updated_at": step.get("progress_updated_at"),
                    "transfer_mode": job.transfer_mode,
                    "source_path": job.source_path,
                    "destination_path": job.destination_path,
                    "percent": progress.get("percent"),
                    "transferred": progress.get("transferred"),
                    "total": progress.get("total"),
                    "speed": progress.get("speed"),
                    "eta": progress.get("eta"),
                    "raw_line": progress.get("raw_line"),
                    "log_step_id": step["id"] if step.get("log_mode") else None,
                    "log_mode": step.get("log_mode"),
                    "delayed_by_antibot": effective_status == "queued"
                    and self._run_delayed_by_antibot(int(step["run_id"])),
                    "can_pause": effective_status == "running",
                    "can_resume": effective_status == "paused",
                    "can_stop": effective_status in {"queued", "running", "paused"},
                }
            )
        return items

    def _total_copy_speed_bytes_per_second(self) -> int:
        total = 0.0
        for item in self._copy_progress_snapshot():
            if str(item.get("status") or "") != "running":
                continue
            speed_raw = str(item.get("speed") or "").strip()
            if not speed_raw:
                continue
            parsed = self._parse_speed_bytes_per_second(speed_raw)
            if parsed is None:
                continue
            total += parsed
        return int(total)

    def _step_rclone_log_path(self, run_id: int, step_id: int) -> Path:
        logs_dir = self.settings.app_root / "data" / "rclone-logs"
        return logs_dir / f"run-{run_id}-step-{step_id}.log"

    def _bind_step_rclone_log(
        self,
        command: list[str],
        run_id: int,
        step_id: int,
        job_key: str | None = None,
        log_mode: str | None = None,
    ) -> list[str]:
        if not command or command[0] != "rclone":
            return command
        effective_log_mode = log_mode if log_mode is not None else self._step_rclone_log_mode(job_key)
        if effective_log_mode is None:
            updated = []
            skip_next = False
            for index, value in enumerate(command):
                if skip_next:
                    skip_next = False
                    continue
                if value == "--log-file":
                    skip_next = index + 1 < len(command)
                    continue
                updated.append(value)
            return updated
        log_path = self._step_rclone_log_path(run_id=run_id, step_id=step_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to reset step log %s", log_path)

        updated = list(command)
        for index, value in enumerate(updated[:-1]):
            if value == "--log-file":
                updated[index + 1] = str(log_path)
                return updated
        updated.extend(["--log-file", str(log_path)])
        return updated

    def _active_operations_snapshot(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for step in self.storage.list_open_run_steps():
            job = self.catalog.get_job(str(step.get("job_key", "")))
            title = job.title or job.description or job.key if job else str(step.get("job_key", "job"))
            items.append(
                {
                    "step_id": step["id"],
                    "run_id": step["run_id"],
                    "job_key": step.get("job_key"),
                    "title": title,
                    "step_kind": step.get("step_kind"),
                    "status": "paused" if self.runner.is_paused(int(step["id"])) else step.get("status"),
                    "profile": step.get("run_profile"),
                    "trigger_type": step.get("run_trigger_type"),
                }
            )
        return items

    def _read_progress_from_rclone_log(self, started_at_raw: str | None, log_path: Path) -> dict[str, Any]:
        if not started_at_raw or not log_path.exists():
            return {}
        try:
            started_at_utc = datetime.fromisoformat(started_at_raw)
            local_tz = ZoneInfo(self.settings.timezone)
            started_at_local = started_at_utc.astimezone(local_tz).replace(tzinfo=None)
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
        except Exception:
            return {}

        latest: dict[str, Any] = {}
        for line in lines:
            parsed = self._parse_rclone_log_progress_line(line)
            if not parsed:
                continue
            line_time = parsed.pop("line_time", None)
            if line_time and line_time < started_at_local:
                continue
            latest = parsed
        return latest

    @staticmethod
    def _parse_rclone_log_progress_line(line: str) -> dict[str, Any] | None:
        prefix = line[:19]
        try:
            line_time = datetime.strptime(prefix, "%Y/%m/%d %H:%M:%S")
        except ValueError:
            return None
        match = RCLONE_LOG_STATS_RE.match(line)
        if match:
            transferred, total, percent, speed, eta = match.groups()
            return {
                "line_time": line_time,
                "raw_line": line.strip(),
                "transferred": transferred.strip(),
                "total": total.strip(),
                "percent": int(percent),
                "speed": speed.strip(),
                "eta": eta.strip(),
            }
        match = RCLONE_LOG_ZERO_RE.match(line)
        if match:
            transferred, total, speed, eta = match.groups()
            return {
                "line_time": line_time,
                "raw_line": line.strip(),
                "transferred": transferred.strip(),
                "total": total.strip(),
                "percent": None,
                "speed": speed.strip(),
                "eta": eta.strip(),
            }
        return None

    @staticmethod
    def _parse_speed_bytes_per_second(raw_value: str) -> float | None:
        normalized = str(raw_value or "").strip()
        if not normalized:
            return None
        if normalized.lower().endswith("/s"):
            normalized = normalized[:-2].strip()
        match = DATA_SIZE_RE.match(normalized)
        if not match:
            return None
        amount = float(match.group(1).replace(",", "."))
        unit = match.group(2).upper()
        factors = {
            "B": 1,
            "KB": 1000,
            "MB": 1000**2,
            "GB": 1000**3,
            "TB": 1000**4,
            "PB": 1000**5,
            "EB": 1000**6,
            "KIB": 1024,
            "MIB": 1024**2,
            "GIB": 1024**3,
            "TIB": 1024**4,
            "PIB": 1024**5,
            "EIB": 1024**6,
        }
        factor = factors.get(unit)
        if factor is None:
            return None
        return amount * factor

    def _step_rclone_log_enabled(self, job_key: str | None) -> bool:
        return self._step_rclone_log_mode(job_key) is not None

    def _step_rclone_log_mode(self, job_key: str | None) -> str | None:
        if self.catalog.logging.rclone_log_enabled:
            return "global"
        if not self.catalog.logging.auto_rclone_log_enabled:
            return None
        normalized_key = str(job_key or "").strip()
        if not normalized_key:
            return None
        return "auto" if self._job_auto_rclone_log_enabled(normalized_key) else None

    def _update_job_auto_rclone_log_state(self, step: dict[str, Any], status: str) -> None:
        if step.get("step_kind") != "job":
            return
        job_key = str(step.get("job_key") or "").strip()
        if not job_key:
            return
        job = self.catalog.get_job(job_key)
        if not job or job.kind != "backup":
            return
        threshold = self.catalog.logging.auto_rclone_log_threshold
        if status == "failed":
            failure_streak = self._job_auto_streak(job_key, success=False) + 1
            self._set_job_auto_streak(job_key, success=False, value=failure_streak)
            self._set_job_auto_streak(job_key, success=True, value=0)
            if (
                self.catalog.logging.auto_rclone_log_enabled
                and failure_streak >= threshold
                and not self._job_auto_rclone_log_enabled(job_key)
            ):
                self._set_job_auto_rclone_log_enabled(job_key, enabled=True)
                logger.info(
                    "enabled auto rclone log for job %s after %s failures",
                    job_key,
                    failure_streak,
                )
            return
        if status == "succeeded":
            success_streak = self._job_auto_streak(job_key, success=True) + 1
            self._set_job_auto_streak(job_key, success=True, value=success_streak)
            self._set_job_auto_streak(job_key, success=False, value=0)
            if (
                self.catalog.logging.auto_rclone_log_enabled
                and self._job_auto_rclone_log_enabled(job_key)
                and success_streak >= threshold
            ):
                self._set_job_auto_rclone_log_enabled(job_key, enabled=False)
                logger.info(
                    "disabled auto rclone log for job %s after %s successes",
                    job_key,
                    success_streak,
                )

    def _job_auto_rclone_log_enabled(self, job_key: str) -> bool:
        raw = self.storage.get_state(f"{AUTO_LOG_ENABLED_STATE_PREFIX}{job_key}")
        return str(raw or "").strip() == "1"

    def _set_job_auto_rclone_log_enabled(self, job_key: str, *, enabled: bool) -> None:
        self.storage.set_state(
            f"{AUTO_LOG_ENABLED_STATE_PREFIX}{job_key}",
            "1" if enabled else "0",
        )

    def _job_auto_streak(self, job_key: str, *, success: bool) -> int:
        prefix = (
            AUTO_LOG_SUCCESS_STREAK_STATE_PREFIX
            if success
            else AUTO_LOG_FAILURE_STREAK_STATE_PREFIX
        )
        raw = self.storage.get_state(f"{prefix}{job_key}")
        try:
            return max(0, int(str(raw or "0").strip()))
        except ValueError:
            return 0

    def _set_job_auto_streak(self, job_key: str, *, success: bool, value: int) -> None:
        prefix = (
            AUTO_LOG_SUCCESS_STREAK_STATE_PREFIX
            if success
            else AUTO_LOG_FAILURE_STREAK_STATE_PREFIX
        )
        self.storage.set_state(f"{prefix}{job_key}", str(max(0, int(value))))

    def _expand_steps(self, jobs: list[JobDefinition]) -> list[RunStepDefinition]:
        expanded: list[RunStepDefinition] = []
        for job in jobs:
            queue_definition = self.catalog.get_queue_definition(job.profile)
            bandwidth_limit = effective_bwlimit(
                self.catalog.bandwidth.limit,
                queue_definition.bandwidth_limit if queue_definition else None,
            )
            expanded.append(
                RunStepDefinition(
                    job_key=job.key,
                    step_kind="job",
                    description=job.description,
                    command=apply_rclone_bwlimit(list(job.command), bandwidth_limit),
                    timeout_seconds=job.timeout_seconds,
                    continue_on_error=job.continue_on_error,
                )
            )
            retention = job.retention.normalized()
            if (
                job.kind == "backup"
                and retention.enabled
                and job.destination_path
            ):
                expanded.append(
                    RunStepDefinition(
                        job_key=job.key,
                        step_kind="retention",
                        description=f"{job.description} / retention",
                        command=JobDefinition.build_retention_command(
                            destination_path=job.destination_path,
                            retention=retention,
                            bandwidth_limit=bandwidth_limit,
                        ),
                        timeout_seconds=job.timeout_seconds,
                        continue_on_error=False,
                    )
                )
        return expanded

    def _scheduler_loop(self) -> None:
        timezone = ZoneInfo(self.settings.timezone)
        while not self._stop_event.is_set():
            now_local = datetime.now(timezone)
            try:
                self._maybe_schedule_jobs(now_local)
            except Exception:
                logger.exception("scheduler tick failed")
            self._stop_event.wait(5)

    def _maybe_schedule_jobs(self, now_local: datetime) -> None:
        jobs = self.catalog.raw_jobs()
        for job in jobs:
            if not job.enabled:
                continue
            schedule_slot = job.schedule.due_slot(now_local)
            if schedule_slot is None:
                continue

            state_key = f"job_schedule_last_slot:{job.key}"
            if self.storage.get_state(state_key) == schedule_slot:
                continue

            if self.catalog.queues.allow_scheduler_queueing and self.storage.has_open_run_for_job(job.key):
                continue

            if self._scheduler_enqueue_blocked(job.profile):
                continue

            try:
                run_id = self.enqueue_job(
                    job_key=job.key,
                    trigger_type="schedule",
                    source="scheduler",
                    requested_by="scheduler",
                    metadata={"slot": schedule_slot, "schedule_mode": job.schedule.mode},
                )
            except ValueError:
                logger.exception("failed to schedule job %s", job.key)
                continue
            self.storage.set_state(state_key, schedule_slot)
            if job.profile == "standard":
                self.storage.set_state("scheduler_last_standard_tick", schedule_slot)
            if job.profile == "heavy":
                self.storage.set_state("scheduler_last_heavy_day", schedule_slot)
            logger.info("scheduled job %s: %s", job.key, run_id)

    def _step_needs_copy_gate(self, step: dict[str, Any]) -> bool:
        if step.get("step_kind") != "job":
            return False
        job = self.catalog.get_job(str(step.get("job_key", "")))
        return bool(job and job.kind == "backup")

    def _wait_for_copy_start_slot(self, run_id: int, step_id: int, step: dict[str, Any]) -> bool:
        job_key = str(step.get("job_key", "") or "unknown")
        while not self._stop_event.is_set():
            current_run = self.storage.get_run(run_id)
            if not current_run or str(current_run.get("status") or "") not in {"queued", "running"}:
                return False
            wait_seconds = self._reserve_copy_start_slot()
            if wait_seconds <= 0:
                return True
            logger.info(
                "delaying copy start for step %s job %s by %.1fs",
                step_id,
                job_key,
                wait_seconds,
            )
            if self._stop_event.wait(min(wait_seconds, 5)):
                return False
        return False

    def _reserve_copy_start_slot(self) -> float:
        with self._copy_start_gate_lock:
            now = datetime.now(timezone.utc)
            next_allowed_at = self._next_copy_start_at_dt(now=now)
            if next_allowed_at is not None:
                wait_seconds = (next_allowed_at - now).total_seconds()
                if wait_seconds > 0:
                    return wait_seconds
            self.storage.set_state(COPY_LAST_STARTED_AT_STATE_KEY, now.isoformat())
            return 0.0

    def _next_copy_start_at(self, now: datetime | None = None) -> str | None:
        next_allowed_at = self._next_copy_start_at_dt(now=now)
        return next_allowed_at.isoformat() if next_allowed_at is not None else None

    def _next_copy_start_at_dt(self, now: datetime | None = None) -> datetime | None:
        current = now or datetime.now(timezone.utc)
        candidates: list[datetime] = []
        if self._copy_starts_allowed_after is not None:
            candidates.append(self._copy_starts_allowed_after)

        last_started_raw = self.storage.get_state(COPY_LAST_STARTED_AT_STATE_KEY)
        if last_started_raw:
            try:
                last_started_at = datetime.fromisoformat(last_started_raw)
                candidates.append(
                    last_started_at
                    + timedelta(seconds=self.settings.copy_min_start_interval_seconds)
                )
            except ValueError:
                logger.warning(
                    "invalid %s state value: %s",
                    COPY_LAST_STARTED_AT_STATE_KEY,
                    last_started_raw,
                )

        if not candidates:
            return None
        next_allowed_at = max(candidates)
        if next_allowed_at <= current:
            return None
        return next_allowed_at

    def _mark_run_delayed(self, queue_name: str, run_id: int) -> None:
        with self._queue_lock:
            self._delayed_runs_by_queue.setdefault(queue_name, set()).add(run_id)

    def _clear_run_delayed(self, queue_name: str, run_id: int) -> None:
        with self._queue_lock:
            delayed = self._delayed_runs_by_queue.get(queue_name)
            if delayed is not None:
                delayed.discard(run_id)

    def _clear_run_delayed_any(self, run_id: int) -> None:
        with self._queue_lock:
            for delayed in self._delayed_runs_by_queue.values():
                delayed.discard(run_id)

    def _run_delayed_by_antibot(self, run_id: int) -> bool:
        with self._queue_lock:
            return any(run_id in delayed for delayed in self._delayed_runs_by_queue.values())
