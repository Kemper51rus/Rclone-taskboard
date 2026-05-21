from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from .rclone_metrics import extract_file_counts, parse_rclone_output_progress_line


@dataclass(frozen=True)
class CommandResult:
    status: str
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    duration_seconds: float


class CommandRunner:
    def __init__(self, dry_run: bool = False, output_tail_chars: int = 8000) -> None:
        self.dry_run = dry_run
        self.output_tail_chars = max(512, output_tail_chars)
        self._lock = threading.RLock()
        self._processes: dict[int, subprocess.Popen[str]] = {}
        self._paused_controls: set[int] = set()
        self._stopped_controls: set[int] = set()
        self._resource_samples: dict[int, tuple[float, int]] = {}
        self._clock_ticks = int(os.sysconf("SC_CLK_TCK") or 100)
        self._page_size = int(os.sysconf("SC_PAGE_SIZE") or 4096)

    def run(
        self,
        command: list[str],
        timeout_seconds: int,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        control_id: int | None = None,
    ) -> CommandResult:
        started = time.perf_counter()

        if self.dry_run:
            duration = time.perf_counter() - started
            return CommandResult(
                status="succeeded",
                exit_code=0,
                stdout_tail=f"dry-run: {' '.join(command)}",
                stderr_tail="",
                duration_seconds=duration,
            )

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            if control_id is not None:
                with self._lock:
                    self._processes[control_id] = process
                    self._paused_controls.discard(control_id)
                    self._stopped_controls.discard(control_id)
                    self._resource_samples[control_id] = (
                        time.perf_counter(),
                        self._process_group_cpu_ticks(process.pid),
                    )
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            lock = threading.Lock()

            def consume_stream(stream: Any, chunks: list[str]) -> None:
                if stream is None:
                    return
                buffer = ""
                while True:
                    char = stream.read(1)
                    if char == "":
                        if buffer:
                            with lock:
                                chunks.append(buffer)
                                self._trim_chunks(chunks)
                            progress = self._parse_progress_line(buffer)
                            if progress and on_progress:
                                on_progress(progress)
                        break
                    buffer += char
                    if char not in {"\n", "\r"}:
                        continue
                    with lock:
                        chunks.append(buffer)
                        self._trim_chunks(chunks)
                    progress = self._parse_progress_line(buffer)
                    if progress and on_progress:
                        on_progress(progress)
                    buffer = ""
                stream.close()

            stdout_thread = threading.Thread(
                target=consume_stream,
                args=(process.stdout, stdout_chunks),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=consume_stream,
                args=(process.stderr, stderr_chunks),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                return_code = process.wait()
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                duration = time.perf_counter() - started
                stdout = "".join(stdout_chunks)
                stderr = "".join(stderr_chunks)
                stderr_msg = f"command timed out after {timeout_seconds}s"
                if stderr:
                    stderr_msg = f"{stderr_msg}\n{stderr}"
                return CommandResult(
                    status="failed",
                    exit_code=None,
                    stdout_tail=self._tail(stdout),
                    stderr_tail=self._tail(stderr_msg),
                    duration_seconds=duration,
                )

            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            duration = time.perf_counter() - started
            if control_id is not None and self.was_stopped(control_id):
                status = "stopped"
            else:
                status = "succeeded" if return_code == 0 else "failed"
            return CommandResult(
                status=status,
                exit_code=return_code,
                stdout_tail=self._tail("".join(stdout_chunks)),
                stderr_tail=self._tail("".join(stderr_chunks)),
                duration_seconds=duration,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            duration = time.perf_counter() - started
            return CommandResult(
                status="failed",
                exit_code=None,
                stdout_tail="",
                stderr_tail=self._tail(f"runner exception: {exc}"),
                duration_seconds=duration,
            )
        finally:
            if control_id is not None:
                with self._lock:
                    self._processes.pop(control_id, None)
                    self._paused_controls.discard(control_id)
                    self._stopped_controls.discard(control_id)
                    self._resource_samples.pop(control_id, None)

    def pause(self, control_id: int) -> bool:
        with self._lock:
            process = self._processes.get(control_id)
            if process is None or process.poll() is not None:
                return False
            self._signal_process(process, signal.SIGSTOP)
            self._paused_controls.add(control_id)
            return True

    def resume(self, control_id: int) -> bool:
        with self._lock:
            process = self._processes.get(control_id)
            if process is None or process.poll() is not None:
                return False
            self._signal_process(process, signal.SIGCONT)
            self._paused_controls.discard(control_id)
            return True

    def stop(self, control_id: int) -> bool:
        with self._lock:
            process = self._processes.get(control_id)
            if process is None or process.poll() is not None:
                return False
            self._stopped_controls.add(control_id)
            self._paused_controls.discard(control_id)
            self._terminate_process(process)
            return True

    def is_paused(self, control_id: int) -> bool:
        with self._lock:
            process = self._processes.get(control_id)
            if process is None or process.poll() is not None:
                return False
            return control_id in self._paused_controls

    def was_stopped(self, control_id: int) -> bool:
        with self._lock:
            return control_id in self._stopped_controls

    def resource_usage(self, control_id: int) -> dict[str, Any] | None:
        with self._lock:
            process = self._processes.get(control_id)
            if process is None or process.poll() is not None:
                self._resource_samples.pop(control_id, None)
                return None
            root_pid = process.pid

        processes = self._process_group_stats(root_pid)
        if not processes:
            return None

        total_ticks = sum(int(item["cpu_ticks"]) for item in processes)
        rss_bytes = sum(int(item["rss_bytes"]) for item in processes)
        now = time.perf_counter()
        cpu_percent: float | None = None
        with self._lock:
            previous = self._resource_samples.get(control_id)
            self._resource_samples[control_id] = (now, total_ticks)
        if previous is not None:
            previous_time, previous_ticks = previous
            elapsed = now - previous_time
            tick_delta = max(0, total_ticks - previous_ticks)
            if elapsed > 0 and self._clock_ticks > 0:
                cpu_percent = (tick_delta / self._clock_ticks) / elapsed * 100

        commands = sorted(
            (
                {
                    "pid": item["pid"],
                    "name": item["name"],
                    "rss_bytes": item["rss_bytes"],
                }
                for item in processes
            ),
            key=lambda item: int(item["rss_bytes"]),
            reverse=True,
        )
        return {
            "pid": root_pid,
            "process_count": len(processes),
            "cpu_percent": round(cpu_percent, 1) if cpu_percent is not None else None,
            "rss_bytes": rss_bytes,
            "commands": commands[:4],
        }

    def _tail(self, value: str) -> str:
        if len(value) <= self.output_tail_chars:
            return value
        return value[-self.output_tail_chars :]

    def _trim_chunks(self, chunks: list[str]) -> None:
        joined_size = sum(len(item) for item in chunks)
        while joined_size > self.output_tail_chars * 2 and len(chunks) > 1:
            joined_size -= len(chunks.pop(0))

    def _process_group_cpu_ticks(self, root_pid: int) -> int:
        return sum(int(item["cpu_ticks"]) for item in self._process_group_stats(root_pid))

    def _process_group_stats(self, root_pid: int) -> list[dict[str, Any]]:
        root_stat = self._read_proc_stat(root_pid)
        if root_stat is None:
            return []
        root_group = int(root_stat["process_group"])
        items: list[dict[str, Any]] = []
        proc_root = Path("/proc")
        try:
            entries = list(proc_root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            stat = self._read_proc_stat(int(entry.name))
            if stat is None or int(stat["process_group"]) != root_group:
                continue
            items.append(stat)
        if not items:
            items.append(root_stat)
        return items

    def _read_proc_stat(self, pid: int) -> dict[str, Any] | None:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return None
        open_paren = raw.find("(")
        close_paren = raw.rfind(")")
        if open_paren < 0 or close_paren <= open_paren:
            return None
        name = raw[open_paren + 1 : close_paren]
        parts = raw[close_paren + 2 :].split()
        if len(parts) < 22:
            return None
        try:
            cpu_ticks = int(parts[11]) + int(parts[12])
            rss_pages = max(0, int(parts[21]))
            return {
                "pid": pid,
                "name": name,
                "parent_pid": int(parts[1]),
                "process_group": int(parts[2]),
                "cpu_ticks": cpu_ticks,
                "rss_bytes": rss_pages * self._page_size,
            }
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clean_7z_current(value: str | None) -> str | None:
        current = str(value or "").strip()
        if not current:
            return None
        current = re.sub(r"^\d+\s+", "", current).strip()
        current = re.sub(r"^[+\-U]\s+", "", current).strip()
        current = re.sub(r"^(?:Scan|Scanning|Compressing|Updating)\s+", "", current, flags=re.IGNORECASE).strip()
        return current or None

    @staticmethod
    def _archive_progress_payload(raw_line: str, percent: int, activity: str | None, current: str | None) -> dict[str, Any]:
        normalized_activity = str(activity or "Compressing").strip() or "Compressing"
        return {
            "raw_line": raw_line,
            "transferred": None,
            "total": None,
            "percent": max(0, min(100, int(percent))),
            "speed": None,
            "eta": None,
            "phase": "archive",
            "activity": normalized_activity,
            "current": CommandRunner._clean_7z_current(current),
            "file_count": None,
            "file_total": None,
        }

    @staticmethod
    def _parse_progress_line(line: str) -> dict[str, Any] | None:
        compact = " ".join(line.strip().split())
        if "Transferred:" not in line:
            prefixed_7z_match = re.search(
                r"^7z:\s*(?P<activity>Compressing|Scanning|Updating)?\s*(?P<percent>\d{1,3})%(?:\s+(?P<current>.*))?$",
                compact,
                re.IGNORECASE,
            )
            if prefixed_7z_match:
                return CommandRunner._archive_progress_payload(
                    compact,
                    int(prefixed_7z_match.group("percent")),
                    prefixed_7z_match.group("activity"),
                    prefixed_7z_match.group("current"),
                )
            native_7z_match = re.search(r"^(?P<percent>\d{1,3})%\s*(?P<current>.*)$", compact)
            if native_7z_match:
                current = native_7z_match.group("current")
                activity = "Scanning" if current.lower().startswith(("scan", "scanning")) else "Compressing"
                return CommandRunner._archive_progress_payload(
                    compact,
                    int(native_7z_match.group("percent")),
                    activity,
                    current,
                )
            seven_zip_match = re.search(
                r"\b(?P<activity>Compressing|Scanning|Updating)\b.*?(?P<percent>\d{1,3})%(?:\s+(?P<current>.*))?",
                compact,
                re.IGNORECASE,
            )
            if seven_zip_match:
                return CommandRunner._archive_progress_payload(
                    compact,
                    int(seven_zip_match.group("percent")),
                    seven_zip_match.group("activity"),
                    seven_zip_match.group("current"),
                )
            rclone_progress = parse_rclone_output_progress_line(compact)
            if rclone_progress:
                return rclone_progress
            return None
        rclone_progress = parse_rclone_output_progress_line(compact)
        if rclone_progress:
            return rclone_progress
        transferred = None
        total = None
        amount_match = re.search(r"Transferred:\s*(.+?)\s*/\s*(.+?)(?:,\s*\d{1,3}%|,\s*-\s*,|$)", compact)
        if amount_match:
            transferred = amount_match.group(1).strip()
            total = amount_match.group(2).strip()
        percent_match = re.search(r"(\d{1,3})%", compact)
        speed_match = re.search(r",\s*([^,]+?/s)(?:,|$)", compact)
        eta_match = re.search(r"ETA\s+([^,]+)", compact)
        progress: dict[str, Any] = {
            "raw_line": compact,
            "transferred": transferred,
            "total": total,
            "percent": int(percent_match.group(1)) if percent_match else None,
            "speed": speed_match.group(1).strip() if speed_match else None,
            "eta": eta_match.group(1).strip() if eta_match else None,
        }
        file_count, file_total = extract_file_counts(compact)
        progress["file_count"] = file_count
        progress["file_total"] = file_total
        if not any(progress.get(key) is not None for key in ("transferred", "total", "percent", "speed", "eta")):
            return None
        return progress

    @staticmethod
    def _signal_process(process: subprocess.Popen[str], sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except Exception:
            process.send_signal(sig)

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        try:
            self._signal_process(process, signal.SIGTERM)
        except Exception:
            process.terminate()
