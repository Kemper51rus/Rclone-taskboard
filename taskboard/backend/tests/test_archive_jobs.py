from __future__ import annotations

from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import ArchiveSettings, JobDefinition  # noqa: E402
from app.jobs_loader import load_catalog, save_catalog  # noqa: E402
from app.domain import JobCatalog  # noqa: E402


class ArchiveJobTests(unittest.TestCase):
    def test_archive_backup_builds_python_7z_upload_command(self) -> None:
        job = JobDefinition(
            key="photos",
            order=10,
            description="Фото",
            timeout_seconds=7200,
            enabled=True,
            continue_on_error=True,
            kind="backup",
            profile="heavy",
            source_path="/media/photos",
            destination_path="mail:/ARCHIVES/photos",
            archive=ArchiveSettings(enabled=True, filename_template="photos-{date}.7z", date_format="%Y-%m-%d"),
        ).validate()

        self.assertEqual(job.command[:3], ["python3", "-m", "app.archive_job"])
        self.assertIn("--source", job.command)
        self.assertIn("/media/photos", job.command)
        self.assertIn("--destination", job.command)
        self.assertIn("mail:/ARCHIVES/photos", job.command)
        self.assertIn("--filename-template", job.command)
        self.assertIn("photos-{date}.7z", job.command)
        self.assertEqual(job.archive.to_dict()["enabled"], True)

    def test_archive_password_and_header_encryption_are_passed_to_helper_command(self) -> None:
        job = JobDefinition(
            key="secure",
            order=1,
            description="Secure archive",
            timeout_seconds=3600,
            enabled=True,
            continue_on_error=True,
            kind="backup",
            profile="standard",
            source_path="/srv/secure",
            destination_path="remote:/archive/secure",
            archive=ArchiveSettings(enabled=True, password="open-secret", encrypt_headers=True),
        ).validate()

        self.assertIn("--password", job.command)
        self.assertIn("open-secret", job.command)
        self.assertIn("--encrypt-headers", job.command)
        self.assertEqual(job.archive.password, "open-secret")
        self.assertTrue(job.archive.encrypt_headers)

    def test_archive_settings_persist_through_catalog(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "jobs.json"
            catalog = JobCatalog(
                jobs=[
                    JobDefinition(
                        key="docs",
                        order=1,
                        description="Docs",
                        timeout_seconds=3600,
                        enabled=True,
                        continue_on_error=True,
                        kind="backup",
                        profile="standard",
                        source_path="/srv/docs",
                        destination_path="remote:/archive/docs",
                        archive=ArchiveSettings(
                            enabled=True,
                            compression_level=9,
                            password="catalog-secret",
                            encrypt_headers=True,
                        ),
                    )
                ],
                profiles={"standard": ["docs"]},
            )

            save_catalog(path, catalog)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["jobs"][0]["archive"]["enabled"], True)
            self.assertEqual(stored["jobs"][0]["archive"]["compression_level"], 9)
            self.assertEqual(stored["jobs"][0]["archive"]["password"], "catalog-secret")
            self.assertEqual(stored["jobs"][0]["archive"]["encrypt_headers"], True)

            loaded = load_catalog(path)
            loaded_job = loaded.get_job("docs")
            self.assertIsNotNone(loaded_job)
            assert loaded_job is not None
            self.assertTrue(loaded_job.archive.enabled)
            self.assertEqual(loaded_job.archive.compression_level, 9)
            self.assertEqual(loaded_job.archive.password, "catalog-secret")
            self.assertTrue(loaded_job.archive.encrypt_headers)


if __name__ == "__main__":
    unittest.main()
