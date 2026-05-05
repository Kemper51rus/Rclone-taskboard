from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo


DATA_SIZE_RE = re.compile(r"^([0-9]+(?:[.,][0-9]+)?)\s*([KMGTPE]?i?B)$", re.IGNORECASE)
XFR_COUNTS_RE = re.compile(r"\(xfr#(\d+)/(\d+)\)")
RCLONE_LOG_STATS_RE = re.compile(
    r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} INFO\s+:\s+(.+?) / (.+?),\s+([0-9-]+)%,\s+([^,]+),\s+ETA\s+(.+?)(?:\s+\(xfr#(\d+)/(\d+)\))?$"
)
RCLONE_LOG_ZERO_RE = re.compile(
    r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} INFO\s+:\s+(.+?) / (.+?),\s+-\s*,\s+([^,]+),\s+ETA\s+(.+?)(?:\s+\(xfr#(\d+)/(\d+)\))?$"
)


def parse_data_size_to_bytes(raw_value: Any) -> int | None:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return None
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
    return int(amount * factor)


def extract_file_counts(raw_value: Any) -> tuple[int | None, int | None]:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return None, None
    match = XFR_COUNTS_RE.search(raw_text)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def enrich_progress(progress: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(progress or {})
    file_count = _normalize_int(payload.get("file_count"))
    file_total = _normalize_int(payload.get("file_total"))
    if file_count is None or file_total is None:
        parsed_count, parsed_total = extract_file_counts(payload.get("raw_line"))
        if file_count is None:
            file_count = parsed_count
        if file_total is None:
            file_total = parsed_total
    payload["file_count"] = file_count
    payload["file_total"] = file_total
    return payload


def parse_rclone_log_progress_line(line: str) -> dict[str, Any] | None:
    prefix = line[:19]
    try:
        line_time = datetime.strptime(prefix, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None

    match = RCLONE_LOG_STATS_RE.match(line)
    if match:
        transferred, total, percent, speed, eta, file_count, file_total = match.groups()
        return {
            "line_time": line_time,
            "raw_line": line.strip(),
            "transferred": transferred.strip(),
            "total": total.strip(),
            "percent": int(percent),
            "speed": speed.strip(),
            "eta": eta.strip(),
            "file_count": _normalize_int(file_count),
            "file_total": _normalize_int(file_total),
        }

    match = RCLONE_LOG_ZERO_RE.match(line)
    if match:
        transferred, total, speed, eta, file_count, file_total = match.groups()
        return {
            "line_time": line_time,
            "raw_line": line.strip(),
            "transferred": transferred.strip(),
            "total": total.strip(),
            "percent": None,
            "speed": speed.strip(),
            "eta": eta.strip(),
            "file_count": _normalize_int(file_count),
            "file_total": _normalize_int(file_total),
        }
    return None


def read_latest_log_progress(
    *,
    started_at_raw: str | None,
    log_path: Path,
    timezone_name: str,
) -> dict[str, Any]:
    if not started_at_raw or not log_path.exists():
        return {}
    try:
        started_at_utc = datetime.fromisoformat(started_at_raw)
        local_tz = ZoneInfo(timezone_name)
        started_at_local = started_at_utc.astimezone(local_tz).replace(tzinfo=None)
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
    except Exception:
        return {}

    latest: dict[str, Any] = {}
    for line in lines:
        parsed = parse_rclone_log_progress_line(line)
        if not parsed:
            continue
        line_time = parsed.pop("line_time", None)
        if line_time and line_time < started_at_local:
            continue
        latest = enrich_progress(parsed)
    return latest


def read_latest_output_progress(output_text: str | None) -> dict[str, Any]:
    lines = str(output_text or "").splitlines()[-300:]
    latest: dict[str, Any] = {}
    for line in lines:
        parsed = parse_rclone_log_progress_line(line)
        if not parsed:
            continue
        parsed.pop("line_time", None)
        latest = enrich_progress(parsed)
    return latest


def extract_transfer_metrics(
    *,
    progress: dict[str, Any] | None,
    output_text: str | None = None,
    log_path: Path | None = None,
    started_at_raw: str | None = None,
    timezone_name: str = "UTC",
) -> dict[str, int | None]:
    merged = enrich_progress(progress)
    needs_log = any(
        merged.get(key) is None
        for key in ("transferred", "total", "file_count", "file_total")
    )
    if needs_log and output_text:
        output_progress = read_latest_output_progress(output_text)
        for key in ("transferred", "total", "file_count", "file_total", "raw_line"):
            if merged.get(key) in (None, "") and output_progress.get(key) not in (None, ""):
                merged[key] = output_progress.get(key)
        needs_log = any(
            merged.get(key) is None
            for key in ("transferred", "total", "file_count", "file_total")
        )

    if needs_log and log_path is not None:
        log_progress = read_latest_log_progress(
            started_at_raw=started_at_raw,
            log_path=log_path,
            timezone_name=timezone_name,
        )
        for key in ("transferred", "total", "file_count", "file_total", "raw_line"):
            if merged.get(key) in (None, "") and log_progress.get(key) not in (None, ""):
                merged[key] = log_progress.get(key)

    return {
        "transferred_bytes": parse_data_size_to_bytes(merged.get("transferred")),
        "total_bytes": parse_data_size_to_bytes(merged.get("total")),
        "file_count": _normalize_int(merged.get("file_count")),
        "file_total": _normalize_int(merged.get("file_total")),
    }


def _normalize_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
