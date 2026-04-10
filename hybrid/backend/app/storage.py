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
                    SELECT id, profile, trigger_type, source, requested_by, status,
                           requested_at, started_at, finished_at, summary, error_count
                    FROM runs
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

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
                           requested_at, started_at, finished_at, summary, error_count
                    FROM runs
                    WHERE id = ?
                    """,
                    (run_id,),
                ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["metadata"] = json.loads(payload.pop("metadata_json") or "{}")
        return payload

    def get_run_step(self, step_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, run_id, step_order, job_key, step_kind, description, command_json, timeout_seconds,
                           continue_on_error, status, started_at, finished_at, duration_seconds,
                           exit_code, progress_json, progress_updated_at, stdout_tail, stderr_tail
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
                           exit_code, progress_json, progress_updated_at, stdout_tail, stderr_tail
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
