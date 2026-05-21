from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import RunStepDefinition  # noqa: E402
from app.runner import CommandRunner  # noqa: E402
from app.storage import Storage  # noqa: E402


class RunMetricsTests(unittest.TestCase):
    def test_runner_parses_7z_percent_progress_line(self) -> None:
        progress = CommandRunner._parse_progress_line("7z: Compressing 42% /media/photos\r")

        self.assertEqual(progress["percent"], 42)
        self.assertEqual(progress["raw_line"], "7z: Compressing 42% /media/photos")
        self.assertEqual(progress["activity"], "Compressing")
        self.assertEqual(progress["current"], "/media/photos")

    def test_runner_parses_native_7z_progress_line(self) -> None:
        progress = CommandRunner._parse_progress_line(" 17% 2048 + /mnt/projects/app/file.js\r")

        self.assertEqual(progress["percent"], 17)
        self.assertEqual(progress["activity"], "Compressing")
        self.assertEqual(progress["current"], "/mnt/projects/app/file.js")

    def test_runner_parses_native_7z_scan_line(self) -> None:
        progress = CommandRunner._parse_progress_line("  0% Scan /mnt/projects\r")

        self.assertEqual(progress["percent"], 0)
        self.assertEqual(progress["activity"], "Scanning")
        self.assertEqual(progress["current"], "/mnt/projects")

    def test_runner_parses_rclone_transfer_bytes_line(self) -> None:
        progress = CommandRunner._parse_progress_line(
            "Transferred:   \t    1.027 MiB / 64 MiB, 2%, 1.027 MiB/s, ETA 1m1s\n"
        )

        self.assertEqual(progress["phase"], "transfer")
        self.assertEqual(progress["transferred"], "1.027 MiB")
        self.assertEqual(progress["total"], "64 MiB")
        self.assertEqual(progress["percent"], 2)
        self.assertEqual(progress["speed"], "1.027 MiB/s")
        self.assertEqual(progress["eta"], "1m1s")

    def test_runner_parses_rclone_current_file_line(self) -> None:
        progress = CommandRunner._parse_progress_line(
            " *                                                                          src.bin: 12% /16Mi, 1.014Mi/s, 13s\n"
        )

        self.assertEqual(progress["phase"], "transfer")
        self.assertEqual(progress["current"], "src.bin")
        self.assertEqual(progress["current_percent"], 12)
        self.assertEqual(progress["current_total"], "16Mi")
        self.assertEqual(progress["speed"], "1.014Mi/s")
        self.assertEqual(progress["eta"], "13s")

    def test_runner_parses_rclone_file_count_line(self) -> None:
        progress = CommandRunner._parse_progress_line("Transferred:            3 / 10, 30%\n")

        self.assertEqual(progress["phase"], "transfer")
        self.assertEqual(progress["file_count"], 3)
        self.assertEqual(progress["file_total"], 10)
        self.assertEqual(progress["file_percent"], 30)

    def test_storage_merges_incremental_progress_updates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "taskboard.db")
            storage.initialize()
            run_id = storage.create_run(
                profile="standard",
                trigger_type="manual",
                source="dashboard",
                requested_by="test",
                metadata={},
            )
            storage.insert_run_steps(
                run_id,
                [
                    RunStepDefinition(
                        job_key="upload",
                        description="Upload",
                        command=["rclone", "copyto", "/tmp/a", "remote:/a"],
                        timeout_seconds=60,
                        continue_on_error=True,
                    )
                ],
            )
            step = storage.list_run_steps(run_id)[0]
            storage.update_step_progress(
                step["id"],
                CommandRunner._parse_progress_line("Transferred: 1 MiB / 10 MiB, 10%, 1 MiB/s, ETA 9s"),
            )
            storage.update_step_progress(
                step["id"],
                CommandRunner._parse_progress_line(" * file.bin: 40% /10Mi, 1 MiB/s, 6s"),
            )

            progress = storage.get_run_step(step["id"])["progress"]

        self.assertEqual(progress["percent"], 10)
        self.assertEqual(progress["transferred"], "1 MiB")
        self.assertEqual(progress["current"], "file.bin")
        self.assertEqual(progress["current_percent"], 40)

    def test_run_history_includes_aggregate_transfer_metrics(self) -> None:
        with TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "taskboard.db")
            storage.initialize()
            run_id = storage.create_run(
                profile="standard",
                trigger_type="manual",
                source="dashboard",
                requested_by="test",
                metadata={},
            )
            storage.insert_run_steps(
                run_id,
                [
                    RunStepDefinition(
                        job_key="photos",
                        description="Photos",
                        command=["python3", "-m", "app.archive_job"],
                        timeout_seconds=60,
                        continue_on_error=True,
                    ),
                    RunStepDefinition(
                        job_key="docs",
                        description="Docs",
                        command=["rclone", "copy", "/src", "remote:/dst"],
                        timeout_seconds=60,
                        continue_on_error=True,
                    ),
                ],
            )
            first, second = storage.list_run_steps(run_id)
            storage.mark_step_finished(
                first["id"],
                status="succeeded",
                duration_seconds=1.0,
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                transferred_bytes=1024,
                total_bytes=2048,
                file_count=1,
                file_total=1,
            )
            storage.mark_step_finished(
                second["id"],
                status="succeeded",
                duration_seconds=1.0,
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                transferred_bytes=3072,
                total_bytes=4096,
                file_count=3,
                file_total=5,
            )

            run = storage.list_runs(limit=1)[0]

        self.assertEqual(run["transferred_bytes"], 4096)
        self.assertEqual(run["total_bytes"], 6144)
        self.assertEqual(run["file_count"], 4)
        self.assertEqual(run["file_total"], 6)


if __name__ == "__main__":
    unittest.main()
