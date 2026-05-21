from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


def _build_archive_name(template: str, date_format: str, source: Path) -> str:
    timestamp = datetime.now().strftime(date_format or "%Y-%m-%d_%H-%M-%S")
    name = (template or "{key}-{date}.7z").format(key=source.stem or source.name, date=timestamp)
    if not name.lower().endswith(".7z"):
        name = f"{name}.7z"
    return name


def _join_remote_path(destination: str, filename: str) -> str:
    cleaned = destination.rstrip("/")
    return f"{cleaned}/{filename}" if cleaned else filename


def _parse_7z_progress_line(line: str, source: Path) -> tuple[int, str, str] | None:
    compact = " ".join(line.strip().split())
    match = re.match(r"^(?P<percent>\d{1,3})%\s*(?P<current>.*)$", compact)
    if not match:
        return None
    percent = max(0, min(100, int(match.group("percent"))))
    current = match.group("current").strip()
    activity = "Scanning" if current.lower().startswith(("scan", "scanning")) else "Compressing"
    current = re.sub(r"^\d+\s+", "", current).strip()
    current = re.sub(r"^[+\-U]\s+", "", current).strip()
    current = re.sub(r"^(?:Scan|Scanning|Compressing|Updating)\s+", "", current, flags=re.IGNORECASE).strip()
    return percent, activity, current or str(source)


def _run_7z_with_progress(command: list[str], source: Path) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    buffer = ""
    last_percent: int | None = None
    last_emit_at = 0.0

    def handle_line(raw_line: str, *, force: bool = False) -> None:
        nonlocal last_percent, last_emit_at
        line = raw_line.strip()
        if not line:
            return
        progress = _parse_7z_progress_line(line, source)
        if progress is None:
            print(line, flush=True)
            return
        percent, activity, current = progress
        now = time.monotonic()
        if not force and percent == last_percent and now - last_emit_at < 1.5:
            return
        print(f"7z: {activity} {percent}% {current}", flush=True)
        last_percent = percent
        last_emit_at = now

    try:
        while True:
            char = process.stdout.read(1)
            if char == "":
                if buffer:
                    handle_line(buffer, force=True)
                break
            buffer += char
            if char not in {"\n", "\r"}:
                continue
            handle_line(buffer)
            buffer = ""
    finally:
        process.stdout.close()

    return_code = process.wait()
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a 7z archive and upload it with rclone")
    parser.add_argument("--source", required=True, help="Local file or directory to archive")
    parser.add_argument("--destination", required=True, help="rclone destination directory, e.g. remote:/archives")
    parser.add_argument("--filename-template", default="{key}-{date}.7z")
    parser.add_argument("--date-format", default="%Y-%m-%d_%H-%M-%S")
    parser.add_argument("--compression-level", type=int, default=5)
    parser.add_argument("--temp-dir", default=None)
    parser.add_argument("--password", default=None, help="7z archive password")
    parser.add_argument("--encrypt-headers", action="store_true", help="Encrypt 7z archive file names")
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"source does not exist: {source}")
    if shutil.which("7z") is None:
        raise RuntimeError("7z executable not found; install p7zip-full or p7zip")
    if shutil.which("rclone") is None:
        raise RuntimeError("rclone executable not found")

    compression_level = max(0, min(9, int(args.compression_level)))
    archive_name = _build_archive_name(args.filename_template, args.date_format, source)
    temp_parent = Path(args.temp_dir).expanduser() if args.temp_dir else None
    if temp_parent:
        temp_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rclone-taskboard-archive-", dir=str(temp_parent) if temp_parent else None) as tmp:
        archive_path = Path(tmp) / archive_name
        seven_zip_command = [
            "7z",
            "a",
            f"-mx={compression_level}",
            "-bsp1",
            str(archive_path),
            str(source),
        ]
        if args.password:
            seven_zip_command.append(f"-p{args.password}")
            if args.encrypt_headers:
                seven_zip_command.append("-mhe=on")
        print(f"7z: Scanning 0% {source}", flush=True)
        _run_7z_with_progress(seven_zip_command, source)
        print(f"7z: Compressing 100% {source}", flush=True)
        subprocess.run(
            [
                "rclone",
                "copyto",
                str(archive_path),
                _join_remote_path(args.destination, archive_name),
                "--stats",
                "1s",
                "--stats-file-name-length",
                "96",
                "--log-level",
                "INFO",
            ],
            check=True,
        )
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except Exception as exc:  # pragma: no cover - CLI guard
        print(f"archive job failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
