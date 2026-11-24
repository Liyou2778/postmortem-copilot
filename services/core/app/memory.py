"""Long-term memory via the ContextDB **v3 memory API** (OAuth session auth).

Two things were discovered by probing, and both matter for anyone reading this
later:

1. The memory API is **``/v3/memories/...``**, not the ``/v1/memories`` that the
   docs imply. ``/v1/memories`` answers ``4007 route is not declared in the DATA
   API contract`` because that route does not exist.
2. Memory routes authenticate with an **OAuth session token** (``Bearer``), not
   with the workspace API key (``Token ctxdb-...``) used by the knowledge API.
   The token comes from the RFC 8628 device flow and expires in 30 minutes, so a
   refresh loop is mandatory for anything long-running.

Contract with the rest of the system:

* **Never evidence.** Recalled memories are rendered as a labelled appendix and
  never enter claim scoring.
* **Best effort.** Any failure degrades to "no memory" and never breaks analysis.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import pathlib
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_OAUTH_BASE = "https://api.cn-hangzhou.agentcontext.aliyuncs.com"
ENV_FILE = pathlib.Path("/opt/copilot/.env")


@dataclass(frozen=True)
class MemoryHit:
    content: str
    score: float
    memory_id: str

    def one_line(self, limit: int = 200) -> str:
        text = " ".join(self.content.split())
        return text if len(text) <= limit else text[:limit] + "…"


class MemoryDisabled:
    """Null object used when ContextDB memory is not configured."""

    enabled = False
    error = ""

    def remember(self, *a, **kw) -> str | None:
        return None

    def recall(self, *a, **kw) -> list[MemoryHit]:
        return []


def _read_env(path: pathlib.Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        env[k.strip()] = v
    return env


def _write_env(path: pathlib.Path, pairs: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key in pairs:
            lines[i] = f"{key}='{pairs[key]}'"
            seen.add(key)
    for k, v in pairs.items():
        if k not in seen:
            lines.append(f"{k}='{v}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


class ContextDbMemory:
    """v3 memory client with automatic OAuth token refresh."""

    def __init__(self, oauth_base: str, access_token: str, refresh_token: str,
                 user_id: str = "liyou", agent_id: str = "postmortem-copilot",
                 *, env_file: pathlib.Path = ENV_FILE, timeout_s: float = 25.0) -> None:
        self._base = (oauth_base or DEFAULT_OAUTH_BASE).rstrip("/")
        self._access = access_token
        self._refresh = refresh_token
        self._user_id = user_id
        self._agent_id = agent_id
        self._env_file = env_file
        self._timeout = timeout_s
        self._expires_at = 0.0
        self._test_factory = None  # injected by tests to avoid real HTTP
        self._error = ""

    # ------------------------------------------------------------- properties
    @property
    def enabled(self) -> bool:
        return bool(self._base and (self._access or self._refresh))

    @property
    def error(self) -> str:
        return self._error

    def _scope(self) -> dict[str, str]:
        return {"user_id": self._user_id, "agent_id": self._agent_id}

    def _client(self, bearer: str | None):
        """HTTP client factory. ``bearer=None`` means an unauthenticated call."""
        if self._test_factory is not None:  # injected in tests
            return self._test_factory()
        import httpx

        headers = {"Content-Type": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return httpx.Client(timeout=self._timeout, headers=headers)

    # ------------------------------------------------------------------ token
    def _do_refresh(self) -> bool:
        if not self._refresh:
            self._error = "no refresh token"
            return False
        try:
            with self._client(None) as c:
                r = c.post(f"{self._base}/v1/auth/token/refresh",
                           json={"refresh_token": self._refresh})
                if r.status_code >= 400:
                    self._error = f"refresh {r.status_code}: {r.text[:120]}"
                    return False
                payload = r.json()
                data = payload.get("data") or payload
                self._access = data.get("access_token") or self._access
                self._refresh = data.get("refresh_token") or self._refresh
                self._expires_at = time.time() + float(data.get("expires_in") or 1800)
                _write_env(self._env_file, {
                    "CONTEXTDB_OAUTH_ACCESS_TOKEN": self._access,
                    "CONTEXTDB_OAUTH_REFRESH_TOKEN": self._refresh,
                })
                log.info("ContextDB OAuth token refreshed")
                return True
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"[:200]
            return False

    def _call(self, path: str, body: dict) -> dict | None:
        """POST with one transparent refresh-and-retry on 401."""
        if not self.enabled:
            return None
        for attempt in (1, 2):
            if not self._access or (self._expires_at and time.time() > self._expires_at - 60):
                if not self._do_refresh() and not self._access:
                    return None
            try:
                with self._client(self._access) as c:
                    r = c.post(f"{self._base}{path}", json=body)
                if r.status_code == 401 and attempt == 1:
                    if self._do_refresh():
                        continue
                    return None
                if r.status_code >= 400:
                    self._error = f"{path} {r.status_code}: {r.text[:150]}"
                    log.warning("ContextDB memory call failed: %s", self._error)
                    return None
                return r.json()
            except Exception as exc:  # noqa: BLE001
                self._error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("ContextDB memory call raised: %s", self._error)
                return None
        return None

    # ----------------------------------------------------------------- memory
    def remember(self, *, task_id: str, service: str, report_md: str,
                 extra: dict | None = None) -> str | None:
        """Queue one incident for background fact extraction."""
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        meta = {"task_id": task_id, "service": service, "source": "postmortem-copilot"}
        if extra:
            meta.update(extra)
        content = (f"[{stamp}] incident postmortem ({service}, task {task_id[:8]}):\n"
                   f"{strip_history_section(report_md)}\n\n"
                   f"metadata: {json.dumps(meta, ensure_ascii=False)}")
        data = self._call("/v3/memories/add/", {
            "messages": [{"role": "user", "content": content}], **self._scope()})
        if not data:
            return None
        event_id = data.get("event_id")
        log.info("Memory queued for extraction: %s (status=%s)", event_id, data.get("status"))
        return event_id

    def recall(self, question: str, top_k: int = 3, threshold: float = 0.30) -> list[MemoryHit]:
        data = self._call("/v3/memories/search/", {
            "query": question, **self._scope(),
            "top_k": max(1, min(top_k, 20)), "threshold": threshold})
        if not data:
            return []
        hits: list[MemoryHit] = []
        for item in data.get("results", []):
            score = float(item.get("score") or item.get("ranking_score") or 0.0)
            hits.append(MemoryHit(
                content=str(item.get("memory") or ""),
                score=score,
                memory_id=str(item.get("id") or ""),
            ))
        return hits


HISTORY_MARKER = "## 历史相似故障（长期记忆召回）"


def strip_history_section(report_md: str) -> str:
    """Remove a previously appended recall appendix before storing a memory.

    Defence in depth: the API layer already passes the original report, but if a
    caller ever hands in the composed one, storing it would make each memory
    contain the previous memories — recall output feeding back into recall input
    and compounding every run.
    """
    idx = report_md.find(HISTORY_MARKER)
    if idx == -1:
        return report_md
    return report_md[:idx].rstrip()


def history_section(hits: list[MemoryHit]) -> str:
    """Render recalled memories as an explicitly non-evidentiary appendix."""
    if not hits:
        return ""
    lines = [
        "", "---", "",
        "## 历史相似故障（长期记忆召回）", "",
        "> 以下内容由长期记忆按语义相似度召回，**仅供背景参考，不作为本次结论的证据**，",
        "> 也不参与本条任务的证据排序。", "",
    ]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. （相似度 {h.score:.2f}）{h.one_line()}")
    lines.append("")
    return "\n".join(lines)


def build(oauth_base: str, access_token: str, refresh_token: str,
          user_id: str, agent_id: str) -> ContextDbMemory | MemoryDisabled:
    if oauth_base and (access_token or refresh_token):
        return ContextDbMemory(oauth_base, access_token, refresh_token, user_id, agent_id)
    return MemoryDisabled()
