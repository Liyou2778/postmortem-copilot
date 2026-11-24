"""Provider-agnostic LLM client.

Speaks the OpenAI-compatible ``/chat/completions`` protocol so any domestic
provider (DeepSeek / 百炼 / 智谱 / Moonshot / vLLM / Ollama) works by changing
three environment variables. When no endpoint is configured, a deterministic
mock is used so the whole orchestration can be tested without network or keys.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx


class LLMError(RuntimeError):
    """Raised when the upstream model fails after all retries."""


@dataclass
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_s: float = 60.0
    max_retries: int = 2
    temperature: float = 0.1

    @classmethod
    def from_env(cls) -> "LLMConfig":
        # Go through the config loader so `.env` is honoured, not just real
        # process environment variables. Reading os.getenv directly here would
        # silently fall back to the mock whenever the app runs outside Docker.
        from .config import get

        return cls(
            base_url=get("LLM_BASE_URL").rstrip("/"),
            api_key=get("LLM_API_KEY"),
            model=get("LLM_MODEL"),
            timeout_s=float(get("LLM_TIMEOUT_S", "60")),
            max_retries=int(get("LLM_MAX_RETRIES", "2")),
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)


class ModelClient:
    """Small chat wrapper with retries and JSON-mode helpers."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()
        self._mock = None if self.config.configured else MockModel()
        # Per-client accounting; the orchestrator snapshots deltas around each
        # step, so cost is attributable to a step, not just the whole task.
        self.usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
        }

    @property
    def is_mock(self) -> bool:
        return self._mock is not None

    def usage_snapshot(self) -> dict[str, int]:
        return dict(self.usage)

    def _record_usage(self, usage: dict[str, Any] | None) -> None:
        usage = usage or {}
        self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        self.usage["total_tokens"] += int(
            usage.get("total_tokens")
            or (int(usage.get("prompt_tokens") or 0)
                + int(usage.get("completion_tokens") or 0))
        )
        self.usage["calls"] += 1

    def chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if self._mock is not None:
            self._record_usage(None)
            return self._mock.respond(system, user, json_mode=json_mode)

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.config.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        last_err: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                with httpx.Client(timeout=self.config.timeout_s) as client:
                    resp = client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                self._record_usage(data.get("usage"))
                return data["choices"][0]["message"]["content"]
            except Exception as exc:  # noqa: BLE001 - retried below
                last_err = exc
                if attempt < self.config.max_retries:
                    time.sleep(0.8 * (2**attempt))
        raise LLMError(f"model call failed after retries: {last_err}") from last_err

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        raw = self.chat(system, user, json_mode=True)
        return _extract_json(raw)


def _extract_json(raw: str) -> dict[str, Any]:
    """Tolerate models that wrap JSON in prose or fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


class MockModel:
    """Deterministic stand-in used when no endpoint is configured.

    It produces role-shaped JSON derived from a hash of the prompt, which makes
    orchestration tests reproducible while still exercising the real parsing,
    validation and conflict-resolution code paths.
    """

    def respond(self, system: str, user: str, *, json_mode: bool = False) -> str:
        seed = int(hashlib.sha256((system + user).encode()).hexdigest()[:8], 16)
        if not json_mode:
            return f"[mock] analysis for seed {seed % 1000}"

        # Cite evidence ids that the prompt actually supplied. Inventing ids
        # would make the contract validator reject every claim, which is the
        # correct behaviour for a real model that hallucinates references.
        refs = _EVIDENCE_ID_RE.findall(user) or _EVIDENCE_ID_RE.findall(system)
        uniq: list[str] = []
        for ref in refs:
            if ref not in uniq:
                uniq.append(ref)
        primary = uniq[0] if uniq else "E-MOCK-1"
        secondary = uniq[1] if len(uniq) > 1 else primary
        third = uniq[2] if len(uniq) > 2 else secondary

        low = (system + user).lower()
        if "timeline" in low or "时间线" in low:
            return json.dumps(
                {
                    "events": [
                        {"at": "2026-11-19T10:00:00Z", "kind": "metric_spike",
                         "title": "支付服务 P99 延迟拐点", "evidence_ref": primary},
                        {"at": "2026-11-19T10:03:00Z", "kind": "deploy",
                         "title": "订单服务发布 v2.31", "evidence_ref": secondary},
                        {"at": "2026-11-19T10:05:00Z", "kind": "log_error",
                         "title": "网关 5xx 突增", "evidence_ref": third},
                    ]
                }
            )
        if "root cause" in low or "root_cause" in low or "根因" in low:
            # Ids must be unique within one response. They used to be
            # ``c{seed % 97}`` / ``c{seed % 89}``, which collide whenever
            # seed%97 == seed%89 (1 in 8633 runs) — and since prompts embed
            # timestamps the seed changes every run, so the duplicate surfaced
            # as a rare, non-reproducible test failure.
            return json.dumps(
                {
                    "claims": [
                        {
                            "id": "c1",
                            "statement": "订单服务 v2.31 把连接池上限从 200 降到 50，导致支付链路排队超时",
                            "confidence": 0.82,
                            "root_cause_rank": 1,
                            "evidence_refs": [primary, secondary],
                        },
                        {
                            "id": "c2",
                            "statement": "支付网关实例数不足，扩容滞后",
                            "confidence": 0.41,
                            "root_cause_rank": 2,
                            "evidence_refs": [third],
                        },
                    ]
                }
            )
        if "review" in low or "verify" in low or "核查" in low or "核查" in system:
            return json.dumps(
                {"verdict": "accept", "issues": [], "checked_claims": 2}
            )
        return json.dumps({"ok": True, "seed": seed % 1000})


_EVIDENCE_ID_RE = re.compile(r"\bE-[A-Za-z0-9][A-Za-z0-9-]{2,}\b")

