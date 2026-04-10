from __future__ import annotations

from dataclasses import dataclass
import os
import re
import signal
import subprocess
import threading
import time
from typing import Any, Callable


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

    def _tail(self, value: str) -> str:
        if len(value) <= self.output_tail_chars:
            return value
        return value[-self.output_tail_chars :]

    def _trim_chunks(self, chunks: list[str]) -> None:
        joined_size = sum(len(item) for item in chunks)
        while joined_size > self.output_tail_chars * 2 and len(chunks) > 1:
            joined_size -= len(chunks.pop(0))

    @staticmethod
    def _parse_progress_line(line: str) -> dict[str, Any] | None:
        if "Transferred:" not in line:
            return None
        compact = " ".join(line.strip().split())
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
