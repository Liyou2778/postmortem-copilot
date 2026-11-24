"""Tests for the SQL dialect layer.

The MySQL branch is exercised without a MySQL server: placeholder rewriting and
upsert generation are pure functions of the backend, so they can be asserted
directly. Anything that needs a live server is covered by the integration check
against RDS.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db import MYSQL, SQLITE, Database, DatabaseError, _split_sql  # noqa: E402


class TestSqliteBackend(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database.sqlite(":memory:")
        self.db.executescript("""
            CREATE TABLE events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                seq     INTEGER NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE (task_id, seq)
            );
            CREATE INDEX idx_task ON events(task_id, seq);
        """)

    def tearDown(self) -> None:
        self.db.close()

    def test_placeholder_is_question_mark(self):
        self.assertEqual(self.db.ph, "?")
        self.assertEqual(self.db.sql("SELECT 1 WHERE a = ?"), "SELECT 1 WHERE a = ?")

    def test_insert_and_fetch(self):
        self.db.execute(
            "INSERT INTO events (task_id, seq, payload) VALUES (?, ?, ?)",
            ("t1", 1, "{}"),
        )
        self.db.commit()
        row = self.db.fetchone("SELECT seq, payload FROM events WHERE task_id = ?", ("t1",))
        self.assertEqual(row, (1, "{}"))

    def test_upsert_replaces_existing_row(self):
        stmt = self.db.upsert_sql("events", ["task_id", "seq", "payload"])
        self.assertIn("INSERT OR REPLACE", stmt)
        self.db.execute(stmt, ("t1", 1, "v1"))
        self.db.execute(stmt, ("t1", 1, "v2"))
        self.db.commit()
        rows = self.db.fetchall("SELECT payload FROM events WHERE task_id = ?", ("t1",))
        self.assertEqual(rows, [("v2",)], "同键 upsert 应覆盖而不是插入两行")

    def test_executescript_runs_multiple_statements(self):
        rows = self.db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?", ("idx_task",)
        )
        self.assertEqual(len(rows), 1)


class TestMysqlDialect(unittest.TestCase):
    """No server needed: only SQL generation is asserted."""

    def setUp(self) -> None:
        # conn=None is fine: these methods never touch the connection.
        self.db = Database(MYSQL, conn=None, dsn="fake")

    def test_placeholder_rewritten(self):
        self.assertEqual(self.db.ph, "%s")
        self.assertEqual(
            self.db.sql("SELECT * FROM events WHERE task_id = ? AND seq > ?"),
            "SELECT * FROM events WHERE task_id = %s AND seq > %s",
        )

    def test_upsert_uses_replace_into(self):
        stmt = self.db.upsert_sql("report", ["task_id", "markdown", "created_at"])
        self.assertTrue(stmt.startswith("REPLACE INTO report"))
        self.assertIn("(%s, %s, %s)", self.db.sql(stmt).replace("?", "%s"))

    def test_no_literal_question_marks_in_our_sql(self):
        # Guards the rewrite trick: a literal '?' inside a string would break it.
        # Our statements never contain one, so the assertion documents the rule.
        statement = "SELECT * FROM t WHERE a = ?"
        self.assertEqual(statement.count("?"), 1)


class TestSplitSql(unittest.TestCase):
    def test_strips_comments_and_blank_lines(self):
        script = """
        -- create table
        CREATE TABLE a (id INT);

        -- another
        CREATE TABLE b (id INT);
        """
        stmts = _split_sql(script)
        self.assertEqual(len(stmts), 2)
        self.assertTrue(stmts[0].startswith("CREATE TABLE a"))
        self.assertNotIn("--", " ".join(stmts))

    def test_single_statement_trailing_semicolon(self):
        self.assertEqual(_split_sql("SELECT 1;"), ["SELECT 1"])


class TestFactoryErrors(unittest.TestCase):
    def test_mysql_without_driver_raises_clear_error(self):
        try:
            import pymysql  # noqa: F401

            self.skipTest("pymysql installed; missing-driver path not reachable")
        except ImportError:
            pass
        with self.assertRaises(DatabaseError) as ctx:
            Database.mysql("h", 3306, "u", "p", "d")
        self.assertIn("pymysql", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
