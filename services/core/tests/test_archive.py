"""Archiving tests.

The archiver must be invisible when unconfigured and must never raise into the
request path when the storage backend misbehaves. Both are asserted here with a
fake bucket so no real OSS credentials are needed.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.archive import ArchiveStore, OssArchiver, OssConfig, build_key  # noqa: E402

TASK = "11111111-2222-3333-4444-555555555555"


class FakeBucket:
    def __init__(self, fail: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = fail

    def put_object(self, key, data, headers=None):  # noqa: ANN001
        if self.fail:
            raise RuntimeError("network down")
        self.objects[key] = data

        class _Result:
            status = 200

        return _Result()

    def sign_url(self, method, key, expires):  # noqa: ANN001
        return f"https://bucket.example/{key}?expires={expires}"


def configured(**over) -> OssConfig:
    base = dict(
        endpoint="oss-cn-hangzhou-internal.aliyuncs.com",
        bucket="copilot-test",
        access_key_id="LTAI5tTESTKEY123456",
        access_key_secret="secret",
        prefix="postmortems/",
    )
    base.update(over)
    return OssConfig(**base)


class TestKeyBuilding(unittest.TestCase):
    def test_key_is_deterministic(self):
        a = build_key("postmortems/", TASK, "report.md")
        b = build_key("postmortems/", TASK, "report.md")
        self.assertEqual(a, b)
        self.assertEqual(a, f"postmortems/{TASK}/report.md")

    def test_prefix_without_slash_is_normalised(self):
        self.assertEqual(build_key("postmortems", TASK, "x"), f"postmortems/{TASK}/x")

    def test_empty_prefix(self):
        self.assertTrue(build_key("", TASK, "x").startswith(f"{TASK}/"))

    def test_tenant_prefix_does_not_mix_teams(self):
        self.assertEqual(
            build_key("postmortems/", TASK, "report.md", tenant_id="team-a"),
            f"postmortems/tenants/team-a/{TASK}/report.md",
        )
        self.assertNotIn(
            "team-b",
            build_key("postmortems/", TASK, "report.md", tenant_id="team-a"),
        )


class TestDisabled(unittest.TestCase):
    def test_missing_config_disables_archiver(self):
        archiver = OssArchiver(OssConfig())
        self.assertFalse(archiver.enabled)
        self.assertIsNone(archiver.put(TASK, "report.md", b"data"))
        self.assertIsNone(archiver.signed_url("k"))

    def test_partial_config_is_not_enough(self):
        cfg = configured(access_key_secret="")
        self.assertFalse(OssArchiver(cfg).enabled)

    def test_masked_never_leaks_secret(self):
        masked = configured().masked()
        self.assertNotIn("secret", str(masked))
        self.assertTrue(str(masked["access_key_id"]).startswith("LTAI5t"))


class TestUpload(unittest.TestCase):
    def setUp(self) -> None:
        # Modules now receive the shared dialect handle (a Database), not a raw
        # sqlite3 connection, because upserts differ per backend.
        from app.db import Database

        self.db = Database.sqlite(":memory:")
        self.store = ArchiveStore(self.db, self.db.lock)
        self.archiver = OssArchiver(configured(), self.store)
        self.bucket = FakeBucket()
        self.archiver._bucket = self.bucket  # inject fake, bypass oss2  # noqa: SLF001

    def test_upload_records_object_key_and_size(self):
        key = self.archiver.put(TASK, "report.md", b"# hi", "text/markdown")
        self.assertEqual(key, f"postmortems/{TASK}/report.md")
        self.assertIn(key, self.bucket.objects)
        entries = self.store.for_task(TASK)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["bytes"], 4)

    def test_archive_task_uploads_all_three_artifacts(self):
        out = self.archiver.archive_task(
            TASK, "# report", b"PK-docx", {"E-TLM-001": {"source": "metric"}}
        )
        self.assertEqual(set(out), {"report.md", "report.docx", "evidence.json"})
        self.assertEqual(len(self.store.for_task(TASK)), 3)

    def test_upload_failure_is_swallowed(self):
        self.archiver._bucket = FakeBucket(fail=True)  # noqa: SLF001
        self.assertIsNone(self.archiver.put(TASK, "report.md", b"x"))
        self.assertIn("RuntimeError", self.archiver.error)
        self.assertEqual(self.store.for_task(TASK), [], "失败不应留下归档记录")

    def test_signed_url(self):
        url = self.archiver.signed_url("postmortems/x/report.md", 600)
        self.assertIn("expires=600", url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
