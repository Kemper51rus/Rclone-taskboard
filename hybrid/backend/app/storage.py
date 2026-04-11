from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from .domain import RunStepDefinition


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    source TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    summary TEXT,
    error_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_requested_at ON runs(requested_at DESC);

CREATE TABLE IF NOT EXISTS run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    step_order INTEGER NOT NULL,
    job_key TEXT NOT NULL,
    step_kind TEXT NOT NULL DEFAULT 'job',
    description TEXT NOT NULL,
    command_json TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    continue_on_error INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    started_at TEXT,
    finished_at TEXT,
    duration_seconds REAL,
    exit_code INTEGER,
    progress_json TEXT,
    progress_updated_at TEXT,
    stdout_tail TEXT,
    stderr_tail TEXT,
    FOREIGN KEY(run_id) REFERENCES runs(id),
    UNIQUE(run_id, step_order)
);

CREATE INDEX IF NOT EXISTS idx_run_steps_run_id ON run_steps(run_id);
CREATE INDEX IF NOT EXISTS idx_run_steps_status ON run_steps(status);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self._connect() as conn:
                conn.executescript(SCHEMA_SQL)
                self._ensure_column(
                    conn,
                    table="run_steps",
                    column="step_kind",
                    definition="TEXT NOT NULL DEFAULT 'job'",
                )
                self._ensure_column(
                    conn,
                    table="run_steps",
                    column="progress_json",
                    definition="TEXT",
                )
                self._ensure_column(
                    conn,
                    table="run_steps",
                    column="progress_updated_at",
                    definition="TEXT",
                )
                self._ensure_column(
                    conn,
                    table="run_steps",
                    column="log_mode",
                    definition="TEXT",
                )
                conn.commit()

    def create_run(
        self,
        profile: str,
        trigger_type: str,
        source: str,
        requested_by: str,
        metadata: dict[str, Any],
    ) -> int:
        requested_at = utc_now_iso()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO runs (
                        profile, trigger_type, source, requested_by, metadata_json, status, requested_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'queued', ?)
                    """,
                    (profile, trigger_type, source, requested_by, metadata_json, requested_at),
                )
                conn.commit()
                return int(cursor.lastrowid)

    def insert_run_steps(self, run_id: int, steps: list[RunStepDefinition]) -> None:
        with self._lock:
            with self._connect() as conn:
                for index, step in enumerate(steps, start=1):
                    conn.execute(
                        """
                        INSERT INTO run_steps (
                            run_id, step_order, job_key, step_kind, description, command_json, timeout_seconds,
                            continue_on_error, status
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')
                        """,
                        (
                            run_id,
                            index,
                            step.job_key,
                            step.step_kind,
                            step.description,
                            json.dumps(step.command, ensure_ascii=False),
                            step.timeout_seconds,
                            1 if step.continue_on_error else 0,
                        ),
                    )
                conn.commit()

    def mark_run_running(self, run_id: int) -> None:
        started_at = utc_now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE runs SET status = 'running', started_at = ? WHERE id = ?",
                    (started_at, run_id),
                )
                conn.commit()

    def mark_run_finished(self, run_id: int, status: str, summary: str, error_count: int) -> None:
        finished_at = utc_now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET status = ?, finished_at = ?, summary = ?, error_count = ?
                    WHERE id = ?
                    """,
                    (status, finished_at, summary, error_count, run_id),
                )
                conn.commit()

    def stop_queued_run(self, run_id: int, summary: str = "stopped before start") -> bool:
        finished_at = utc_now_iso()
        with self._lock:
            with self._connect() as conn:
                run_row = conn.execute(
                    "SELECT status FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if run_row is None or str(run_row["status"]) != "queued":
                    return False
                conn.execute(
                    """
                    UPDATE run_steps
                    SET status = 'stopped', finished_at = ?, progress_updated_at = COALESCE(progress_updated_at, ?)
                    WHERE run_id = ? AND status = 'queued'
                    """,
                    (finished_at, finished_at, run_id),
                )
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'stopped', finished_at = ?, summary = ?, error_count = 1
                    WHERE id = ?
                    """,
                    (finished_at, summary, run_id),
                )
                conn.commit()
                return True

    def recover_incomplete_runs(self) -> int:
        recovered = 0
        finished_at = utc_now_iso()
        with self._lock:
            with self._connect() as conn:
                open_runs = conn.execute(
                    """
                    SELECT id
                    FROM runs
                    WHERE status IN ('queued', 'running')
                    ORDER BY id ASC
                    """
                ).fetchall()
                for row in open_runs:
                    run_id = int(row["id"])
                    conn.execute(
                        """
                        UPDATE run_steps
                        SET status = 'stopped', finished_at = ?, progress_updated_at = COALESCE(progress_updated_at, ?)
                        WHERE run_id = ? AND status = 'running'
                        """,
                        (finished_at, finished_at, run_id),
                    )
                    conn.execute(
                        """
                        UPDATE run_steps
                        SET status = 'skipped', finished_at = ?
                        WHERE run_id = ? AND status = 'queued'
                        """,
                        (finished_at, run_id),
                    )
                    conn.execute(
                        """
                        UPDATE runs
                        SET status = 'stopped',
                            finished_at = ?,
                            summary = ?,
                            error_count = CASE WHEN error_count < 1 THEN 1 ELSE error_count END
                        WHERE id = ?
                        """,
                        (
                            finished_at,
                            "recovered after service restart; unfinished steps marked stopped",
                            run_id,
                        ),
                    )
                    recovered += 1
                conn.commit()
        return recovered

    def mark_step_running(self, step_id: int) -> None:
        started_at = utc_now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE run_steps
                    SET status = 'running', started_at = ?, progress_json = NULL, progress_updated_at = NULL
                    WHERE id = ?
                    """,
                    (started_at, step_id),
                )
                conn.commit()

    def set_step_log_mode(self, step_id: int, log_mode: str | None) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE run_steps SET log_mode = ? WHERE id = ?",
                    ((str(log_mode).strip() or None) if log_mode is not None else None, step_id),
                )
                conn.commit()

    def update_step_progress(self, step_id: int, progress: dict[str, Any]) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE run_steps
                    SET progress_json = ?, progress_updated_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(progress, ensure_ascii=False), utc_now_iso(), step_id),
                )
                conn.commit()

    def mark_step_finished(
        self,
        step_id: int,
        status: str,
        duration_seconds: float,
        exit_code: int | None,
        stdout_tail: str,
        stderr_tail: str,
    ) -> None:
        finished_at = utc_now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE run_steps
                    SET status = ?, finished_at = ?, duration_seconds = ?, exit_code = ?,
                        stdout_tail = ?, stderr_tail = ?, progress_updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        finished_at,
                        duration_seconds,
                        exit_code,
                        stdout_tail,
                        stderr_tail,
                        finished_at,
                        step_id,
                    ),
                )
                conn.commit()

    def skip_pending_steps(self, run_id: int, after_step_order: int) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE run_steps
                    SET status = 'skipped', finished_at = ?
                    WHERE run_id = ? AND step_order > ? AND status = 'queued'
                    """,
                    (utc_now_iso(), run_id, after_step_order),
                )
                conn.commit()

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT
                        r.id,
                        r.profile,
                        r.trigger_type,
                        r.source,
                        r.requested_by,
                        r.status,
                        r.requested_at,
                        r.started_at,
                        r.finished_at,
                        r.summary,
                        r.error_count,
                        (
                            SELECT rs.id
                            FROM run_steps rs
                            WHERE rs.run_id = r.id
                              AND rs.log_mode IS NOT NULL
                            ORDER BY CASE WHEN rs.status = 'failed' THEN 0 ELSE 1 END, rs.step_order ASC
                            LIMIT 1
                        ) AS log_step_id,
                        (
                            SELECT CASE
                                WHEN COUNT(DISTINCT rs.job_key) = 1 THEN MIN(rs.job_key)
                                ELSE NULL
                            END
                            FROM run_steps rs
                            WHERE rs.run_id = r.id
                              AND rs.step_kind = 'job'
                        ) AS job_key
                    FROM runs r
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                items: list[dict[str, Any]] = []
                for row in rows:
                    payload = dict(row)
                    payload["failure_reason"] = self._run_failure_reason(
                        conn,
                        run_id=int(payload["id"]),
                    )
                    items.append(payload)
        return items

    def clear_run_history(self) -> dict[str, int]:
        with self._lock:
            with self._connect() as conn:
                run_row = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM runs
                    WHERE status NOT IN ('queued', 'running')
                    """
                ).fetchone()
                step_row = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM run_steps
                    WHERE run_id IN (
                        SELECT id
                        FROM runs
                        WHERE status NOT IN ('queued', 'running')
                    )
                    """
                ).fetchone()
                runs_deleted = int(run_row["count"]) if run_row else 0
                steps_deleted = int(step_row["count"]) if step_row else 0
                conn.execute(
                    """
                    DELETE FROM run_steps
                    WHERE run_id IN (
                        SELECT id
                        FROM runs
                        WHERE status NOT IN ('queued', 'running')
                    )
                    """
                )
                conn.execute(
                    """
                    DELETE FROM runs
                    WHERE status NOT IN ('queued', 'running')
                    """
                )
                conn.commit()
        return {
            "runs_deleted": runs_deleted,
            "steps_deleted": steps_deleted,
        }

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, profile, trigger_type, source, requested_by, metadata_json, status,
                           requested_at, started_at, finished_at, summary, error_count,
                           (
                               SELECT rs.id
                               FROM run_steps rs
                               WHERE rs.run_id = runs.id
                                 AND rs.log_mode IS NOT NULL
                               ORDER BY CASE WHEN rs.status = 'failed' THEN 0 ELSE 1 END, rs.step_order ASC
                               LIMIT 1
                           ) AS log_step_id
                    FROM runs
                    WHERE id = ?
                    """,
                    (run_id,),
                ).fetchone()
                if row is None:
                    return None
                payload = dict(row)
                payload["metadata"] = json.loads(payload.pop("metadata_json") or "{}")
                payload["failure_reason"] = self._run_failure_reason(conn, run_id=run_id)
                return payload

    def get_run_step(self, step_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, run_id, step_order, job_key, step_kind, description, command_json, timeout_seconds,
                           continue_on_error, status, started_at, finished_at, duration_seconds,
                           exit_code, progress_json, progress_updated_at, stdout_tail, stderr_tail, log_mode
                    FROM run_steps
                    WHERE id = ?
                    """,
                    (step_id,),
                ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["command"] = json.loads(payload.pop("command_json") or "[]")
        payload["progress"] = json.loads(payload.pop("progress_json") or "null")
        payload["continue_on_error"] = bool(payload.get("continue_on_error"))
        return payload

    def list_run_steps(self, run_id: int) -> list[dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, run_id, step_order, job_key, step_kind, description, command_json, timeout_seconds,
                           continue_on_error, status, started_at, finished_at, duration_seconds,
                           exit_code, progress_json, progress_updated_at, stdout_tail, stderr_tail, log_mode
                    FROM run_steps
                    WHERE run_id = ?
                    ORDER BY step_order ASC
                    """,
                    (run_id,),
                ).fetchall()
        steps: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["command"] = json.loads(payload.pop("command_json") or "[]")
            payload["progress"] = json.loads(payload.pop("progress_json") or "null")
            payload["continue_on_error"] = bool(payload.get("continue_on_error"))
            steps.append(payload)
        return steps

    def list_open_run_steps(self) -> list[dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT
                        rs.id,
                        rs.run_id,
                        rs.step_order,
                        rs.job_key,
                        rs.step_kind,
                        rs.description,
                        rs.status,
                        rs.started_at,
                        rs.finished_at,
                        rs.duration_seconds,
                        rs.exit_code,
                        rs.progress_json,
                        rs.progress_updated_at,
                        rs.log_mode,
                        r.profile AS run_profile,
                        r.trigger_type AS run_trigger_type,
                        r.status AS run_status,
                        r.requested_at AS run_requested_at
                    FROM run_steps rs
                    JOIN runs r ON r.id = rs.run_id
                    WHERE r.status IN ('queued', 'running')
                      AND rs.status IN ('queued', 'running')
                    ORDER BY r.id DESC, rs.step_order ASC
                    """
                ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["progress"] = json.loads(payload.pop("progress_json") or "null")
            items.append(payload)
        return items

    def list_rclone_log_steps(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT
                        rs.id,
                        rs.run_id,
                        rs.step_order,
                        rs.job_key,
                        rs.step_kind,
                        rs.description,
                        rs.command_json,
                        rs.status,
                        rs.started_at,
                        rs.finished_at,
                        rs.duration_seconds,
                        rs.exit_code,
                        rs.log_mode,
                        r.profile AS run_profile,
                        r.trigger_type AS run_trigger_type,
                        r.status AS run_status,
                        r.requested_at AS run_requested_at,
                        r.started_at AS run_started_at,
                        r.finished_at AS run_finished_at
                    FROM run_steps rs
                    JOIN runs r ON r.id = rs.run_id
                    ORDER BY COALESCE(rs.started_at, r.requested_at) DESC, rs.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["command"] = json.loads(payload.pop("command_json") or "[]")
            command = payload.get("command") or []
            if not command or str(command[0]).strip() != "rclone":
                continue
            items.append(payload)
        return items

    def open_run_count(self, profile: str | None = None) -> int:
        with self._lock:
            with self._connect() as conn:
                if profile:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM runs
                        WHERE profile = ? AND status IN ('queued', 'running')
                        """,
                        (profile,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) AS count FROM runs WHERE status IN ('queued', 'running')"
                    ).fetchone()
        return int(row["count"]) if row else 0

    def has_open_run_for_job(self, job_key: str) -> bool:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM runs r
                    JOIN run_steps rs ON rs.run_id = r.id
                    WHERE r.status IN ('queued', 'running')
                      AND rs.job_key = ?
                      AND rs.status IN ('queued', 'running')
                    LIMIT 1
                    """,
                    (job_key,),
                ).fetchone()
        return row is not None

    def latest_job_run_map(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT
                        rs.job_key,
                        r.id AS run_id,
                        r.status AS run_status,
                        r.trigger_type,
                        r.requested_at,
                        r.started_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY rs.job_key
                            ORDER BY COALESCE(r.started_at, r.requested_at) DESC, r.id DESC
                        ) AS row_number
                    FROM run_steps rs
                    JOIN runs r ON r.id = rs.run_id
                    WHERE rs.step_kind = 'job'
                    """
                ).fetchall()
        items: dict[str, dict[str, Any]] = {}
        for row in rows:
            if int(row["row_number"]) != 1:
                continue
            items[str(row["job_key"])] = {
                "run_id": int(row["run_id"]),
                "status": str(row["run_status"]),
                "trigger_type": str(row["trigger_type"]),
                "requested_at": row["requested_at"],
                "started_at": row["started_at"],
            }
        return items

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO kv_state(key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, utc_now_iso()),
                )
                conn.commit()

    def get_state(self, key: str) -> str | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO events(event_type, payload_json, created_at) VALUES (?, ?, ?)",
                    (event_type, json.dumps(payload, ensure_ascii=False), utc_now_iso()),
                )
                conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _run_failure_reason(self, conn: sqlite3.Connection, run_id: int) -> str | None:
        row = conn.execute(
            """
            SELECT job_key, step_kind, status, exit_code, stdout_tail, stderr_tail
            FROM run_steps
            WHERE run_id = ?
              AND status IN ('failed', 'stopped')
            ORDER BY CASE WHEN status = 'failed' THEN 0 ELSE 1 END, step_order ASC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None

        label = str(row["job_key"] or "").strip() or (
            "step" if str(row["step_kind"] or "") != "job" else "job"
        )
        excerpt = self._tail_excerpt(str(row["stderr_tail"] or "")) or self._tail_excerpt(
            str(row["stdout_tail"] or "")
        )
        if excerpt:
            return f"{label}: {excerpt}"

        status = str(row["status"] or "").strip()
        exit_code = row["exit_code"]
        if status == "failed" and exit_code is not None:
            return f"{label}: exit code {exit_code}"
        if status == "stopped":
            return f"{label}: остановлено"
        if status:
            return f"{label}: {status}"
        return label

    @staticmethod
    def _tail_excerpt(value: str) -> str | None:
        if not value:
            return None
        lines = [Storage._normalize_tail_line(line) for line in value.splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            return None
        for line in reversed(lines):
            if "Transferred:" not in line:
                return Storage._trim_excerpt(line)
        return Storage._trim_excerpt(lines[-1])

    @staticmethod
    def _normalize_tail_line(line: str) -> str:
        return " ".join(str(line or "").split())

    @staticmethod
    def _trim_excerpt(value: str, limit: int = 220) -> str:
        cleaned = str(value or "").strip()
        if len(cleaned) <= limit:
            return cleaned
        return f"{cleaned[: limit - 1].rstrip()}…"

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        *,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        existing = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row["name"] == column for row in existing):
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
