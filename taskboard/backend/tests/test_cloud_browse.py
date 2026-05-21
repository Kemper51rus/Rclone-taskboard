from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cloud_browse import _browse_cloud_directories, _cloud_browse_remote_path  # noqa: E402
from app.domain import CloudSettings  # noqa: E402


class CloudBrowseTests(unittest.TestCase):
    def test_cloud_browse_remote_path_combines_root_and_relative_path(self) -> None:
        cloud = CloudSettings(key="mail", title="Mail", remote_name="mail", root_path="/BACKUPS")

        self.assertEqual(_cloud_browse_remote_path(cloud, "photos/2026"), "mail:/BACKUPS/photos/2026")
        self.assertEqual(_cloud_browse_remote_path(cloud, ""), "mail:/BACKUPS")

    def test_browse_cloud_directories_returns_relative_subpaths_and_parent(self) -> None:
        cloud = CloudSettings(key="mail", title="Mail", remote_name="mail", root_path="/BACKUPS")
        completed = Mock(
            returncode=0,
            stdout=json.dumps([
                {"Name": "2025", "Path": "2025", "IsDir": True},
                {"Name": "ignore.txt", "Path": "ignore.txt", "IsDir": False},
                {"Name": "2026", "Path": "2026", "IsDir": True},
            ]),
            stderr="",
        )

        with patch("app.cloud_browse.subprocess.run", return_value=completed) as run:
            payload = _browse_cloud_directories(cloud, "photos")

        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            ["rclone", "lsjson", "--dirs-only", "mail:/BACKUPS/photos"],
        )
        self.assertEqual(payload["path"], "photos")
        self.assertEqual(payload["parent"], "")
        self.assertEqual(
            payload["directories"],
            [
                {"name": "2025", "path": "photos/2025", "remote_path": "mail:/BACKUPS/photos/2025"},
                {"name": "2026", "path": "photos/2026", "remote_path": "mail:/BACKUPS/photos/2026"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
