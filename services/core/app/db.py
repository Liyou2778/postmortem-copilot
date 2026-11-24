"""One SQL surface, two backends (SQLite for dev/tests, MySQL for production).

Why a wrapper instead of an ORM: the domain layer contains ~15 hand-written SQL
statements. An ORM would rewrite them all and hide the access patterns that make
this system auditable. A thin wrapper keeps the SQL readable while absorbing the
three real differences between the backends:

1. **Placeholders** — ``?`` vs ``%s``. Callers keep writing ``?``; the wrapper
   rewrites for MySQL. Safe here because no statement contains a literal ``?``.
2. **Upsert** — ``INSERT OR REPLACE`` vs ``REPLACE INTO``.
3. **Transactions** — SQLite needs explicit ``commit()``; MySQL runs with
   autocommit, so ``commit()`` becomes a no-op.

The wrapper also owns a single lock. Both backends are used from multiple
threads (FastAPI threadpool + heartbeat threads), and every module shares one
connection, so serialising access at one place is both simpler and safer than
per-module locking.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Iterable, Sequence

SQLITE = "sqlite"
MYSQL = "mysql"


class DatabaseError(RuntimeError):
    """Backend-agnostic wrapper so callers never import driver exceptions."""


class Database:
    """A connection plus the dialect needed to talk to it."""

    def __init__(self, backend: str, conn: Any, dsn: str = "") -> None:
        self.backend = backend
        self.conn = conn
        self.dsn = dsn
        self.lock = threading.RLock()
        self._autocommit = backend == MYSQL

    # ------------------------------------------------------------- properties
    @property
    def ph(self) -> str:
        return "?" if self.backend == SQLITE else "%s"

    def sql(self, statement: str) -> str:
        """Translate a ``?``-style statement for the active backend."""
        if self.backend == MYSQL:
            return statement.replace("?", "%s")
        return statement

    # ---------------------------------------------------------------- queries
    def execute(self, statement: str, params: Sequence[Any] | None = None):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(self.sql(statement), tuple(params or ()))
            return cur

    def cursor(self):
        """Expose a raw cursor so legacy code can keep calling ``conn.cursor()``."""
        return self.conn.cursor()

    @staticmethod
    def integrity_errors() -> tuple[type[BaseException], ...]:
        """Driver-specific unique-constraint exceptions, for backend-agnostic catch."""
        errors: list[type[BaseException]] = [sqlite3.IntegrityError]
        try:
            import pymysql

            errors.append(pymysql.err.IntegrityError)
        except ImportError:  # pragma: no cover - driver only on MySQL deployments
            pass
        return tuple(errors)

    def executescript(self, script: str) -> None:
        """Run multi-statement DDL. Only used at startup."""
        with self.lock:
            cur = self.conn.cursor()
            if self.backend == SQLITE:
                cur.executescript(script)
                self.conn.commit()
                return
            for stmt in _split_sql(script):
                cur.execute(stmt)
            self.conn.commit()

    def fetchone(self, statement: str, params: Sequence[Any] | None = None):
        cur = self.execute(statement, params)
        return cur.fetchone()

    def fetchall(self, statement: str, params: Sequence[Any] | None = None):
        cur = self.execute(statement, params)
        return cur.fetchall()

    def commit(self) -> None:
        if not self._autocommit:
            with self.lock:
                self.conn.commit()

    def rollback(self) -> None:
        with self.lock:
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001 - rollback is best effort
                pass

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ----------------------------------------------------------------- upsert
    def insert_ignore_sql(self, table: str, columns: Sequence[str]) -> str:
        """Statement that silently skips a duplicate-key insert.

        SQLite spells this ``INSERT OR IGNORE``; MySQL spells it ``INSERT
        IGNORE``. Used by the work queue for at-most-once enqueueing.
        """
        cols = ", ".join(columns)
        marks = ", ".join(["?"] * len(columns))
        verb = "INSERT OR IGNORE" if self.backend == SQLITE else "INSERT IGNORE"
        return f"{verb} INTO {table} ({cols}) VALUES ({marks})"

    def upsert_sql(self, table: str, columns: Sequence[str]) -> str:
        """Return an upsert statement for the given table/columns.

        ``REPLACE INTO`` is chosen for MySQL because every table that needs an
        upsert here is keyed by its full uniqueness constraint (no partial
        updates), which makes REPLACE semantics identical to INSERT OR REPLACE.
        """
        cols = ", ".join(columns)
        marks = ", ".join(["?"] * len(columns))
        verb = "INSERT OR REPLACE" if self.backend == SQLITE else "REPLACE"
        return f"{verb} INTO {table} ({cols}) VALUES ({marks})"

    # ------------------------------------------------------------- factories
    @classmethod
    def sqlite(cls, path: str = ":memory:") -> "Database":
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return cls(SQLITE, conn, dsn=path)

    @classmethod
    def mysql(cls, host: str, port: int, user: str, password: str, database: str,
              *, connect_timeout: int = 8, autocommit: bool = True) -> "Database":
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise DatabaseError(
                "pymysql is required for DB_BACKEND=mysql: pip install pymysql"
            ) from exc

        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=autocommit,
            connect_timeout=connect_timeout,
            cursorclass=pymysql.cursors.Cursor,
        )
        return cls(MYSQL, conn, dsn=f"{user}@{host}:{port}/{database}")


def _split_sql(script: str) -> list[str]:
    """Split a DDL script into statements.

    Comments are stripped *before* splitting on ';'. Doing it the other way
    round breaks whenever a comment contains a semicolon, because the comment
    gets cut in half and its tail is then mistaken for a statement.
    """
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


# Service catalog DDL.
#
# Deliberately written with MySQL-compatible types so the *same* script runs on
# both backends: SQLite accepts VARCHAR/DOUBLE/INT through type affinity, and
# MySQL is the production target. That removes the usual "two schemas drift
# apart" failure mode for a brand-new feature.
CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS datasource (
    name VARCHAR(128) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    base_url VARCHAR(512) NOT NULL,
    mock INT NOT NULL DEFAULT 0,
    description VARCHAR(512) NULL,
    enabled INT NOT NULL DEFAULT 1,
    updated_at DOUBLE NOT NULL,
    updated_by VARCHAR(128) NOT NULL DEFAULT 'system',
    PRIMARY KEY (name)
);

CREATE TABLE IF NOT EXISTS service_catalog (
    name VARCHAR(128) NOT NULL,
    display_name VARCHAR(255) NULL,
    owner VARCHAR(128) NULL,
    tier VARCHAR(16) NULL,
    description VARCHAR(1024) NULL,
    enabled INT NOT NULL DEFAULT 1,
    updated_at DOUBLE NOT NULL,
    updated_by VARCHAR(128) NOT NULL DEFAULT 'system',
    PRIMARY KEY (name)
);

CREATE TABLE IF NOT EXISTS service_binding (
    service_name VARCHAR(128) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    datasource_name VARCHAR(128) NOT NULL,
    selector VARCHAR(1024) NULL,
    metric VARCHAR(128) NULL,
    secondary_metric VARCHAR(128) NULL,
    -- M10 metric pack: gauge/counter semantics + extra metrics as JSON array.
    metric_type VARCHAR(16) NOT NULL DEFAULT 'gauge',
    secondary_metric_type VARCHAR(16) NOT NULL DEFAULT 'gauge',
    extra_metrics MEDIUMTEXT NULL,
    -- M10.3 impact business mapping (users_per_request / cost_per_minute).
    impact_policy MEDIUMTEXT NULL,
    mode VARCHAR(16) NOT NULL,
    enabled INT NOT NULL DEFAULT 1,
    updated_at DOUBLE NOT NULL,
    updated_by VARCHAR(128) NOT NULL DEFAULT 'system',
    PRIMARY KEY (service_name, kind)
);
"""


# Complete MySQL schema. The per-module SCHEMA constants are SQLite-shaped
# (TEXT primary keys, AUTOINCREMENT) and are invalid on MySQL, so when the
# backend is MySQL this single script creates everything once at startup and the
# modules skip their own DDL. Keeping one authoritative copy avoids drift.
MYSQL_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id BIGINT NOT NULL AUTO_INCREMENT,
    task_id VARCHAR(64) NOT NULL,
    seq INT NOT NULL,
    type VARCHAR(64) NOT NULL,
    payload MEDIUMTEXT NOT NULL,
    actor VARCHAR(128) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_events_task_seq (task_id, seq),
    KEY idx_events_task (task_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS idempotency (
    `key` VARCHAR(191) NOT NULL,
    task_id VARCHAR(64) NOT NULL,
    step_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    result MEDIUMTEXT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_create_idempotency (
    `key` VARCHAR(191) NOT NULL,
    task_id VARCHAR(64) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS leases (
    task_id VARCHAR(64) NOT NULL,
    step_id VARCHAR(64) NOT NULL,
    owner VARCHAR(128) NOT NULL,
    fence INT NOT NULL,
    expires_at BIGINT NOT NULL,
    PRIMARY KEY (task_id, step_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS compensations (
    id VARCHAR(64) NOT NULL,
    task_id VARCHAR(64) NOT NULL,
    step_id VARCHAR(64) NOT NULL,
    action VARCHAR(64) NOT NULL,
    payload MEDIUMTEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (id),
    KEY idx_comp_task (task_id, status),
    KEY idx_comp_step (step_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS report (
    task_id VARCHAR(64) NOT NULL,
    markdown MEDIUMTEXT NOT NULL,
    created_at DOUBLE NOT NULL,
    PRIMARY KEY (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS step_output (
    task_id VARCHAR(64) NOT NULL,
    step_id VARCHAR(64) NOT NULL,
    role VARCHAR(64) NOT NULL,
    payload MEDIUMTEXT NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (task_id, step_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_evidence (
    task_id VARCHAR(64) NOT NULL,
    evidence_id VARCHAR(64) NOT NULL,
    payload MEDIUMTEXT NOT NULL,
    PRIMARY KEY (task_id, evidence_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS archive (
    task_id VARCHAR(64) NOT NULL,
    kind VARCHAR(64) NOT NULL,
    object_key VARCHAR(255) NOT NULL,
    bytes BIGINT NOT NULL,
    created_at DOUBLE NOT NULL,
    PRIMARY KEY (task_id, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
""" + CATALOG_DDL


def is_mysql(handle: Any) -> bool:
    """True when the handle is a MySQL-backed Database."""
    return getattr(handle, "backend", SQLITE) == MYSQL


def ensure_schema(handle: Any) -> None:
    """Create the full schema when running on MySQL; no-op on SQLite.

    SQLite keeps its per-module DDL (they are created by each store), so this is
    only the MySQL path.
    """
    if is_mysql(handle):
        handle.executescript(MYSQL_DDL)


def from_settings(settings) -> Database:  # noqa: ANN001 - avoid config import cycle
    """Build the configured backend. Defaults to SQLite."""
    if getattr(settings, "db_backend", SQLITE) == MYSQL:
        return Database.mysql(
            host=settings.db_host,
            port=int(settings.db_port),
            user=settings.db_user,
            password=settings.db_password,
            database=settings.db_name,
        )
    return Database.sqlite(settings.db_path)
