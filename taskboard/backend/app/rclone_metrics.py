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
RCLONE_LOG_PREFIX_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \w+\s+:\s*(.*)$")
RCLONE_OUTPUT_TRANSFER_BYTES_RE = re.compile(
    r"^Transferred:\s*(?P<transferred>.+?)\s*/\s*(?P<total>.+?),\s*(?P<percent>\d{1,3}|-)%?,\s*(?P<speed>[^,]+),\s*ETA\s*(?P<eta>.+)$"
)
RCLONE_OUTPUT_TRANSFER_FILES_RE = re.compile(
    r"^Transferred:\s*(?P<file_count>\d+)\s*/\s*(?P<file_total>\d+),\s*(?P<file_percent>\d{1,3})%$"
)
RCLONE_OUTPUT_CURRENT_RE = re.compile(
    r"^\*\s+(?P<current>.+?):\s*(?P<current_percent>\d{1,3})%\s*/(?P<current_total>[^,]+),\s*(?P<speed>[^,]+)(?:,\s*(?P<eta>.+))?$"
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


def merge_progress(existing: dict[str, Any] | None, update: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(existing or {})
    for key, value in dict(update or {}).items():
        if value in (None, ""):
            continue
        merged[key] = value
    return enrich_progress(merged)


def strip_rclone_log_prefix(line: str) -> str:
    match = RCLONE_LOG_PREFIX_RE.match(str(line or "").strip())
    return (match.group(1) if match else str(line or "")).strip()


def parse_rclone_output_progress_line(line: str) -> dict[str, Any] | None:
    compact = " ".join(strip_rclone_log_prefix(line).split())
    if not compact:
        return None

    match = RCLONE_OUTPUT_TRANSFER_FILES_RE.match(compact)
    if match:
        return {
            "raw_line": compact,
            "phase": "transfer",
            "activity": "Transferring",
            "file_count": _normalize_int(match.group("file_count")),
            "file_total": _normalize_int(match.group("file_total")),
            "file_percent": _normalize_int(match.group("file_percent")),
        }

    match = RCLONE_OUTPUT_TRANSFER_BYTES_RE.match(compact)
    if match:
        percent_raw = match.group("percent")
        return {
            "raw_line": compact,
            "transferred": match.group("transferred").strip(),
            "total": match.group("total").strip(),
            "percent": None if percent_raw == "-" else int(percent_raw),
            "speed": match.group("speed").strip(),
            "eta": match.group("eta").strip(),
            "phase": "transfer",
            "activity": "Transferring",
        }

    match = RCLONE_OUTPUT_CURRENT_RE.match(compact)
    if match:
        return {
            "raw_line": compact,
            "phase": "transfer",
            "activity": "Transferring",
            "current": match.group("current").strip(),
            "current_percent": _normalize_int(match.group("current_percent")),
            "current_total": match.group("current_total").strip(),
            "speed": match.group("speed").strip(),
            "eta": (match.group("eta") or "").strip() or None,
        }

    return None


def parse_rclone_log_progress_line(line: str) -> dict[str, Any] | None:
    prefix = line[:19]
    try:
        line_time = datetime.strptime(prefix, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return parse_rclone_output_progress_line(line)

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
    return parse_rclone_output_progress_line(line)


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
        latest = merge_progress(latest, parsed)
    return latest


def read_latest_output_progress(output_text: str | None) -> dict[str, Any]:
    lines = str(output_text or "").splitlines()[-300:]
    latest: dict[str, Any] = {}
    for line in lines:
        parsed = parse_rclone_log_progress_line(line)
        if not parsed:
            continue
        parsed.pop("line_time", None)
        latest = merge_progress(latest, parsed)
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
