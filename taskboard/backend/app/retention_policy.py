"""Persistent cleanup cadence and incremental expired-date selection.

Never purge directories or remove the min-age safety guard. A successful full
cleanup supplies a watermark; regular cleanups revisit the dates that expired
since that watermark (plus overlap), instead of every historical cloud branch.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from .domain import JobDefinition, parse_retention_age


@dataclass(frozen=True)
class RetentionPlan:
    command: list[str]
    mode: str
    state_key: str
    started_at: str
    paths: list[str]
    reason: str = ""
    next_due_at: str | None = None

    @property
    def skipped(self) -> bool:
        return self.mode.startswith("skip_")

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "started_at": self.started_at, "paths": self.paths,
                "reason": self.reason, "next_due_at": self.next_due_at}


def retention_state_key(job: JobDefinition) -> str:
    retention = job.retention.normalized()
    # Cadence changes recalculate from existing success, not restart the clock.
    scan = asdict(retention.directory_scan)
    scan.pop("full_scan_interval_hours", None)
    scan.pop("full_scan_enabled", None)
    payload = {"destination": job.destination_path, "min_age": retention.min_age,
               "exclude": retention.exclude, "extra_args": retention.extra_args, "scan": scan}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    return f"retention_policy:{job.key}:{digest}"


def timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or ""))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def decode_state(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _without_runtime_flags(command: list[str]) -> list[str]:
    flags = {"--bwlimit", "--log-file", "--log-level", "--stats", "--stats-file-name-length"}
    boolean_flags = {"--stats-one-line"}
    output = []
    i = 0
    while i < len(command):
        part = command[i]
        if part in flags:
            i += 2
        elif part.split("=", 1)[0] in flags or part in boolean_flags:
            i += 1
        else:
            output.append(part)
            i += 1
    return output


def command_is_dry_run(command: list[str]) -> bool:
    if not command or command[0] != "rclone":
        return False
    value_flags = {
        "--filter", "-f", "--exclude", "--include", "--filter-from", "--exclude-from",
        "--include-from", "--files-from", "--files-from-raw", "--exclude-if-present",
        "--max-age", "--min-age", "--bwlimit", "--log-file", "--log-level", "--config",
        "--stats", "--stats-file-name-length", "--timeout", "--contimeout", "--transfers",
        "--checkers", "--retries", "--low-level-retries", "--retries-sleep", "--dump",
        "--tpslimit", "--tpslimit-burst", "--min-size", "--max-size", "--max-depth",
    }
    i = 2
    while i < len(command):
        token = command[i]
        if token == "--":
            break
        if token in value_flags:
            i += 2
            continue
        flag, _, value = token.partition("=")
        if flag == "--dry-run" and value.lower() != "false":
            return True
        # pflag accepts -n=true and combined short booleans (-nv/-vn).
        # Only option-like ASCII clusters, not filter values '- **/name/**'.
        if re.fullmatch(r"-[A-Za-z]*n[A-Za-z]*", flag) and value.lower() != "false":
            return True
        i += 1
    return False


def compatible_full_cleanup(job: JobDefinition, step: dict[str, Any]) -> dict[str, str] | None:
    """Only adopt an actual successful, unfiltered cleanup with same semantics."""
    if step.get("status") != "succeeded" or step.get("step_kind") != "retention":
        return None
    if str(step.get("stdout_tail") or "").startswith("dry-run:"):
        return None
    started = timestamp(step.get("started_at"))
    finished = timestamp(step.get("finished_at"))
    if not started or not finished or finished < started:
        return None
    command = step.get("command", [])
    if command_is_dry_run(command):
        return None
    expected = JobDefinition.build_retention_command(job.destination_path, job.retention)
    if _without_runtime_flags(command) != _without_runtime_flags(expected):
        return None
    return {"last_success_at": finished.isoformat(), "last_cleanup_started_at": started.isoformat(),
            "last_full_scan_at": finished.isoformat()}


def policy_status(job: JobDefinition, state: dict[str, Any]) -> dict[str, Any]:
    retention = job.retention.normalized()
    last = timestamp(state.get("last_success_at"))
    due = last + timedelta(hours=retention.interval_hours) if last and retention.enabled and retention.interval_enabled else None
    return {"enabled": retention.enabled, "interval_enabled": retention.interval_enabled,
            "last_success_at": last.isoformat() if last else None,
            "last_cleanup_started_at": state.get("last_cleanup_started_at"),
            "last_full_scan_at": state.get("last_full_scan_at"),
            "next_due_at": due.isoformat() if due else None}


def _date_filter_command(command: list[str], paths: list[str]) -> list[str]:
    exclusions: list[str] = []
    output: list[str] = []
    i = 0
    while i < len(command):
        part = command[i]
        if part == "--exclude":
            if i + 1 >= len(command):
                raise ValueError("retention --exclude requires a pattern")
            exclusions.append(command[i + 1]); i += 2
        elif part.startswith("--exclude="):
            exclusions.append(part.split("=", 1)[1]); i += 1
        elif part == "--fast-list" or part.startswith("--fast-list="):
            i += 1
        else:
            output.append(part); i += 1
    for pattern in exclusions:
        output.extend(["--filter", f"- {pattern}"])
    for path in paths:
        output.extend(["--filter", f"+ /{path}/**"])
    output.extend(["--filter", "- /**"])
    return output


def build_retention_plan(job: JobDefinition, command: list[str], *, now: datetime,
                         state: dict[str, Any], copy_succeeded: bool = True) -> RetentionPlan:
    if now.tzinfo is None:
        raise ValueError("retention policy requires aware timestamps")
    job = job.validate()
    now = now.astimezone(timezone.utc)
    settings = job.retention
    key = retention_state_key(job)
    started_at = now.isoformat()
    if not settings.enabled:
        return RetentionPlan(command, "skip_disabled", key, started_at, [], "Очистка отключена")
    if not copy_succeeded:
        return RetentionPlan(command, "skip_copy_failed", key, started_at, [], "Копирование не завершилось успешно; очистка пропущена")
    # Stale queued selectors/age overrides must not weaken deletion safety.
    expected = JobDefinition.build_retention_command(job.destination_path, settings)
    if _without_runtime_flags(command) != _without_runtime_flags(expected):
        raise ValueError("queued retention command differs from current policy; enqueue the job again")
    last_success = timestamp(state.get("last_success_at"))
    if last_success and last_success <= now and settings.interval_enabled:
        due = last_success + timedelta(hours=settings.interval_hours)
        if now < due:
            return RetentionPlan(command, "skip_interval", key, started_at, [],
                                 "Очистка отложена до следующего интервала", due.isoformat())
    scan = settings.directory_scan
    if not scan.enabled:
        return RetentionPlan(list(command), "full", key, started_at, [])
    last_started = timestamp(state.get("last_cleanup_started_at"))
    last_full = timestamp(state.get("last_full_scan_at"))
    # Without a trustworthy baseline, never guess the oldest remote date.
    full = (not last_started or not last_full or not last_success
            or last_started > now or last_full > now or last_success > now
            or (scan.full_scan_enabled and now >= last_full + timedelta(hours=scan.full_scan_interval_hours)))
    if full:
        return RetentionPlan(list(command), "full", key, started_at, [], "Полная контрольная очистка; min-age и исключения сохранены")
    age = parse_retention_age(settings.min_age)
    zone = ZoneInfo(scan.timezone)
    # Use the START of the previous successful cleanup, not its completion:
    # rclone computes the age cutoff when it starts. Completion can skip gaps.
    try:
        start = (last_started - age).astimezone(zone).date() - timedelta(days=scan.overlap_days)
        end = (now - age).astimezone(zone).date()
    except (OverflowError, ValueError) as exc:
        raise ValueError("retention age/overlap exceeds supported calendar range") from exc
    if (end - start).days > 3650 or end < start:
        return RetentionPlan(list(command), "full", key, started_at, [], "Большой разрыв между очистками: безопасная полная сверка")
    paths = []
    day = start
    while day <= end:
        paths.append(day.strftime(scan.path_template))
        day += timedelta(days=1)
    paths = list(dict.fromkeys(paths))
    return RetentionPlan(_date_filter_command(command, paths), "window", key, started_at, paths,
                         "Выборочная очистка недавно истёкших дат; min-age и исключения сохранены")


def successful_state(plan: RetentionPlan, previous: dict[str, Any], finished_at: datetime) -> dict[str, Any]:
    if plan.skipped:
        return dict(previous)
    updated = {**previous, "last_success_at": finished_at.astimezone(timezone.utc).isoformat(),
               "last_cleanup_started_at": plan.started_at}
    if plan.mode == "full":
        updated["last_full_scan_at"] = updated["last_success_at"]
    return updated
