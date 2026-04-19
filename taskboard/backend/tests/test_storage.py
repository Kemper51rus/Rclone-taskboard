from __future__ import annotations

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.storage import Storage  # noqa: E402


def open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


class StorageDatabaseTests(unittest.TestCase):
    def test_repeated_state_calls_do_not_leak_file_descriptors(self) -> None:
        if not Path("/proc/self/fd").exists():
            self.skipTest("/proc/self/fd is not available")

        with TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "taskboard.db")
            storage.initialize()

            before = open_fd_count()
            for index in range(200):
                storage.set_state("leak-check", str(index))
                self.assertEqual(storage.get_state("leak-check"), str(index))
            after = open_fd_count()

        self.assertEqual(after, before)

    def test_database_diagnostics_and_maintenance(self) -> None:
        with TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "taskboard.db")
            storage.initialize()
            storage.set_state("sample", "value")

            diagnostics = storage.database_diagnostics()
            self.assertEqual(diagnostics["journal_mode"], "wal")
            self.assertGreater(diagnostics["database_size_bytes"], 0)
            self.assertGreaterEqual(diagnostics["total_size_bytes"], diagnostics["database_size_bytes"])
            self.assertIn("reclaimable_bytes", diagnostics)

            checkpoint = storage.checkpoint_database()
            self.assertTrue(checkpoint["completed"])
            self.assertGreaterEqual(checkpoint["freed_bytes"], 0)

            vacuum = storage.vacuum_database()
            self.assertTrue(vacuum["completed"])
            self.assertGreaterEqual(vacuum["freed_bytes"], 0)
            self.assertIsNotNone(vacuum["after"]["last_vacuum_at"])


if __name__ == "__main__":
    unittest.main()
