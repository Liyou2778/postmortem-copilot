"""Object-storage archiving for reports and evidence.

Why archive at all: a locally stored report dies with the disk. OSS gives the
artifacts a durable home and a shareable link, which is what a postmortem needs
once it leaves the service and gets attached to a review.

Design constraints:

* **Optional.** If OSS is not configured the service must behave exactly as
  before. Archiving is a side effect, never a dependency of the API call.
* **Internal endpoint.** Same-region ECS + `-internal` endpoint means traffic
  does not leave the VPC and is not billed as egress.
* **Lazy import.** `oss2` pulls a large dependency tree; importing it only when
  configured keeps startup fast and tests independent.
"""
from __future__ import annotations

from .db import is_mysql

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive (
    task_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,      -- report.md | report.docx | evidence.json
    object_key TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, kind)
);
"""


@dataclass(frozen=True)
class OssConfig:
    endpoint: str = ""
    bucket: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    prefix: str = "postmortems/"

    @property
    def configured(self) -> bool:
        return bool(
            self.endpoint and self.bucket and self.access_key_id and self.access_key_secret
        )

    def masked(self) -> dict[str, Any]:
        key = self.access_key_id
        return {
            "endpoint": self.endpoint or "<unset>",
            "bucket": self.bucket or "<unset>",
            "access_key_id": f"{key[:6]}...{key[-4:]}" if len(key) > 12 else ("<unset>" if not key else "***"),
            "prefix": self.prefix,
            "configured": self.configured,
        }


def build_key(prefix: str, task_id: str, kind: str, tenant_id: str = "") -> str:
    """Deterministic object key: <prefix>tenants/<tenant>/<task_id>/<kind>.

    Legacy callers without a tenant keep the old <prefix><task_id>/<kind>
    layout; tenant-aware writes get an explicit prefix so listings do not mix
    teams by convention alone.
    """
    clean_prefix = prefix if prefix.endswith("/") or not prefix else prefix + "/"
    if tenant_id:
        clean_tenant = tenant_id.strip().replace("/", "_")
        return f"{clean_prefix}tenants/{clean_tenant}/{task_id}/{kind}"
    return f"{clean_prefix}{task_id}/{kind}"


class ArchiveStore:
    """Bookkeeping: which artifacts of which task live where."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock
        if not is_mysql(self._conn):
            self._conn.executescript(ARCHIVE_SCHEMA)
        self._conn.commit()

    def save(self, task_id: str, kind: str, object_key: str, size: int) -> None:
        with self._lock:
            self._conn.execute(
                self._conn.upsert_sql(
                    "archive", ["task_id", "kind", "object_key", "bytes", "created_at"]
                ),
                (task_id, kind, object_key, size, time.time()),
            )
            self._conn.commit()

    def for_task(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, object_key, bytes FROM archive WHERE task_id = ? ORDER BY kind",
                (task_id,),
            ).fetchall()
        return [{"kind": r[0], "object_key": r[1], "bytes": r[2]} for r in rows]


class OssArchiver:
    """Thin wrapper so the rest of the app never imports oss2 directly."""

    def __init__(self, config: OssConfig, store: ArchiveStore | None = None) -> None:
        self.config = config
        self.store = store
        self._bucket = None
        self._error = ""

    @property
    def enabled(self) -> bool:
        return self.config.configured

    @property
    def error(self) -> str:
        return self._error

    def _get_bucket(self):
        if self._bucket is not None:
            return self._bucket
        import oss2  # lazy: only when configured

        auth = oss2.Auth(self.config.access_key_id, self.config.access_key_secret)
        self._bucket = oss2.Bucket(auth, self.config.endpoint, self.config.bucket)
        return self._bucket

    def put(self, task_id: str, kind: str, data: bytes,
            content_type: str = "application/octet-stream",
            tenant_id: str = "") -> str | None:
        """Upload one artifact. Returns the object key, or None when disabled/failed."""
        if not self.enabled:
            return None
        key = build_key(self.config.prefix, task_id, kind, tenant_id=tenant_id)
        try:
            # No `import oss2` here: the bucket may already be injected (tests),
            # and _get_bucket() is the only place that needs the SDK.
            bucket = self._get_bucket()
            result = bucket.put_object(
                key, data,
                headers={"Content-Type": content_type},
            )
            if getattr(result, "status", 200) != 200:
                self._error = f"put_object status {getattr(result, 'status', '?')}"
                log.warning("OSS upload failed for %s: %s", key, self._error)
                return None
            if self.store is not None:
                self.store.save(task_id, kind, key, len(data))
            return key
        except Exception as exc:  # noqa: BLE001 - archiving must never break a request
            self._error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("OSS upload failed for %s: %s", key, self._error)
            return None

    def read(self, object_key: str) -> bytes | None:
        """Fetch an object's bytes (used to proxy downloads through the API).

        Proxying beats redirecting to a signed URL here: the bucket is reached
        over the *internal* endpoint from the ECS, and a signed URL built from
        that endpoint is not resolvable from the public internet. Streaming
        through the API keeps the bucket fully private and avoids exposing the
        OSS hostname to browsers at all.
        """
        if not self.enabled:
            return None
        try:
            bucket = self._get_bucket()
            return bucket.get_object(object_key).read()
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("OSS read failed for %s: %s", object_key, self._error)
            return None

    def signed_url(self, object_key: str, expires_s: int = 3600) -> str | None:
        if not self.enabled:
            return None
        try:
            bucket = self._get_bucket()
            return bucket.sign_url("GET", object_key, expires_s)
        except Exception as exc:  # noqa: BLE001
            log.warning("OSS sign_url failed: %s", exc)
            return None

    def archive_task(self, task_id: str, markdown: str, docx: bytes | None,
                     evidence: dict[str, Any] | None,
                     tenant_id: str = "") -> dict[str, str | None]:
        """Archive all artifacts of one task. Never raises."""
        out: dict[str, str | None] = {}
        out["report.md"] = self.put(
            task_id, "report.md", markdown.encode("utf-8"),
            "text/markdown; charset=utf-8", tenant_id=tenant_id,
        )
        if docx:
            out["report.docx"] = self.put(
                task_id, "report.docx", docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                tenant_id=tenant_id,
            )
        if evidence:
            payload = json.dumps(evidence, ensure_ascii=False, indent=2).encode("utf-8")
            out["evidence.json"] = self.put(
                task_id, "evidence.json", payload, "application/json; charset=utf-8",
                tenant_id=tenant_id,
            )
        return out
