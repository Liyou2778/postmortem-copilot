"""Long-term memory tests (ContextDB v3 memory API).

The contract that matters: memory must be invisible when unconfigured, must
never raise into an analysis, must refresh an expiring OAuth token, and recalled
history must be rendered as a clearly non-evidentiary appendix.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.memory import (  # noqa: E402
    ContextDbMemory,
    MemoryDisabled,
    MemoryHit,
    build,
    history_section,
    strip_history_section,
)


class FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, json: dict | None = None):
        self.calls.append((url, json or {}))
        return self.responses.pop(0)


def make(responses: list[FakeResponse],
         refresh_token: str = "rt") -> tuple[ContextDbMemory, FakeClient]:
    m = ContextDbMemory("https://api.example", "at", refresh_token, "liyou", "pm-copilot")
    client = FakeClient(responses)
    m._test_factory = lambda: client  # type: ignore[assignment]  # noqa: SLF001
    return m, client


class TestDisabled(unittest.TestCase):
    def test_no_tokens_returns_null_object(self):
        m = build("https://api.example", "", "", "liyou", "pm")
        self.assertIsInstance(m, MemoryDisabled)
        self.assertFalse(m.enabled)
        self.assertIsNone(m.remember(task_id="t", service="s", report_md="x"))
        self.assertEqual(m.recall("q"), [])


class TestRemember(unittest.TestCase):
    def test_remember_posts_messages_to_v3_add(self):
        m, client = make([FakeResponse(200, {"event_id": "ev-1", "status": "PENDING"})])
        event_id = m.remember(task_id="11112222-3333", service="order-service",
                              report_md="# 复盘\n根因 X")
        self.assertEqual(event_id, "ev-1")
        url, body = client.calls[0]
        self.assertTrue(url.endswith("/v3/memories/add/"), url)
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertIn("根因 X", body["messages"][0]["content"])
        self.assertEqual(body["user_id"], "liyou")
        self.assertEqual(body["agent_id"], "pm-copilot")

    def test_remember_returns_none_on_server_error(self):
        m, _ = make([FakeResponse(500, {"detail": "boom"})])
        self.assertIsNone(m.remember(task_id="t1", service="s", report_md="a"))
        self.assertIn("500", m.error)


class TestRecall(unittest.TestCase):
    def test_recall_parses_v3_results(self):
        m, client = make([FakeResponse(200, {"results": [
            {"id": "m1", "memory": "[2026-11-22] user: 连接池耗尽导致延迟", "score": 0.93},
            {"id": "m2", "memory": "无关内容", "score": 0.10},
        ]})])
        hits = m.recall("连接池", top_k=3, threshold=0.3)
        self.assertEqual(len(hits), 2, "阈值由服务端处理，客户端保留全部返回")
        self.assertEqual(hits[0].score, 0.93)
        url, body = client.calls[0]
        self.assertTrue(url.endswith("/v3/memories/search/"))
        self.assertEqual(body["query"], "连接池")
        self.assertEqual(body["top_k"], 3)
        self.assertEqual(body["threshold"], 0.3)

    def test_recall_survives_failure(self):
        m, _ = make([FakeResponse(503, {})])
        self.assertEqual(m.recall("q"), [])


class TestTokenRefresh(unittest.TestCase):
    def test_401_triggers_refresh_then_retry(self):
        m, client = make([
            FakeResponse(401, {"detail": "expired"}),
            FakeResponse(200, {"data": {"access_token": "new-at", "refresh_token": "new-rt",
                                        "expires_in": 1800}}),
            FakeResponse(200, {"results": []}),
        ])
        hits = m.recall("q")
        self.assertEqual(hits, [])
        urls = [u for u, _ in client.calls]
        self.assertIn("https://api.example/v1/auth/token/refresh", urls,
                      "401 后应先刷新 token 再重试")
        self.assertEqual(m._access, "new-at")  # noqa: SLF001
        self.assertEqual(m._refresh, "new-rt")  # noqa: SLF001

    def test_no_refresh_token_returns_empty(self):
        m, _ = make([FakeResponse(401, {})], refresh_token="")
        self.assertEqual(m.recall("q"), [])


class TestFeedbackLoop(unittest.TestCase):
    """Recall output must never be stored back as recall input."""

    def test_strip_removes_appendix(self):
        composed = "# 复盘\n根因 X\n\n---\n\n## 历史相似故障（长期记忆召回）\n\n> 仅供背景参考…\n"
        stripped = strip_history_section(composed)
        self.assertIn("根因 X", stripped)
        self.assertNotIn("历史相似故障", stripped)

    def test_strip_is_noop_without_appendix(self):
        self.assertEqual(strip_history_section("# 复盘\n根因 X"), "# 复盘\n根因 X")

    def test_remember_never_stores_appendix(self):
        m, client = make([FakeResponse(200, {"event_id": "ev", "status": "PENDING"})])
        composed = ("# 复盘\n本次根因 Y\n\n" + history_section([MemoryHit("旧记忆内容", 0.9, "m1")]))
        m.remember(task_id="t", service="s", report_md=composed)
        content = client.calls[0][1]["messages"][0]["content"]
        self.assertIn("本次根因 Y", content)
        self.assertNotIn("历史相似故障", content)
        self.assertNotIn("旧记忆内容", content)


class TestHistorySection(unittest.TestCase):
    def test_empty_hits_render_nothing(self):
        self.assertEqual(history_section([]), "")

    def test_section_marks_memory_as_non_evidence(self):
        out = history_section([MemoryHit("连接池问题导致延迟", 0.93, "m1")])
        self.assertIn("历史相似故障", out)
        self.assertIn("不作为本次结论的证据", out)
        self.assertIn("0.93", out)

    def test_long_content_truncated(self):
        self.assertIn("…", history_section([MemoryHit("x" * 500, 0.5, "m")]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
