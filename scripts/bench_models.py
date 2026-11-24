r"""Benchmark candidate models on the real agent prompt.

Measures latency, token usage and JSON validity for the same prompt each agent
will send. Output is what justifies the default model choice in the design doc.

Run: .\.venv\Scripts\python.exe scripts\bench_models.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

import httpx  # noqa: E402

from app.config import load_settings  # noqa: E402

MODELS = ["deepseek-flash", "deepseek-v4-pro"]

SYSTEM = (
    "你是互联网平台的 SRE 根因分析助手。只输出 JSON，不要输出解释文字。"
    "所有结论必须引用给定的证据编号，不得编造编号。"
)

USER = """服务: order-service
时间窗: 30m
指标数据: {"metric":"latency_p99_ms","points":[{"t":"10:00","v":240},{"t":"10:02","v":890},{"t":"10:04","v":1520}],"anomaly":{"t":"10:02","v":890}}
变更: {"changes":[{"change_id":"CHG-20261119-031","at":"10:03","summary":"order-service v2.31: 连接池上限 200 -> 50"}]}
日志: 3 条错误，首条: connection pool exhausted

可用证据:
- E-TLM-001 | source=metric | P99 延迟由 240ms 升至 890ms
- E-CHG-001 | source=change | order-service v2.31 连接池上限 200 -> 50
- E-LOG-001 | source=log | connection pool exhausted

请输出根因候选，格式: {"claims":[{"id":"c1","statement":"...","confidence":0.0,"root_cause_rank":1,"evidence_refs":["E-..."]}]}
"""


def call(base: str, key: str, model: str, timeout: float = 90) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    started = time.perf_counter()
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
    elapsed = (time.perf_counter() - started) * 1000
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {}) or {}

    valid_json = False
    refs_ok = False
    claims = 0
    try:
        parsed = json.loads(content)
        valid_json = True
        claims = len(parsed.get("claims") or [])
        known = {"E-TLM-001", "E-CHG-001", "E-LOG-001"}
        refs = [r for c in (parsed.get("claims") or []) for r in (c.get("evidence_refs") or [])]
        refs_ok = bool(refs) and all(r in known for r in refs)
    except json.JSONDecodeError:
        parsed = {}

    return {
        "model": model,
        "latency_ms": int(elapsed),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "valid_json": valid_json,
        "refs_valid": refs_ok,
        "claims": claims,
        "preview": (content or "")[:120].replace("\n", " "),
    }


def main() -> int:
    s = load_settings()
    if not s.llm_configured:
        print("!! 未配置模型，无法基准测试")
        return 2

    print("=" * 78)
    print("模型基准测试（同一 prompt，真实调用）")
    print("=" * 78)

    results = []
    for model in MODELS:
        try:
            r = call(s.llm_base_url, s.llm_api_key, model)
        except Exception as exc:  # noqa: BLE001
            print(f"\n{model:18} FAIL {type(exc).__name__}: {str(exc)[:80]}")
            continue
        results.append(r)
        print(f"\n{model}")
        print(f"  延迟        : {r['latency_ms']} ms")
        print(f"  token       : 输入 {r['prompt_tokens']} / 输出 {r['completion_tokens']}")
        print(f"  JSON 合法   : {r['valid_json']}")
        print(f"  证据引用合法: {r['refs_valid']}")
        print(f"  结论条数    : {r['claims']}")
        print(f"  片段        : {r['preview']}")

    if not results:
        return 1

    print("\n" + "=" * 78)
    print("对比")
    print("=" * 78)
    print(f"{'model':18} {'latency':>9} {'in_tok':>8} {'out_tok':>8} {'json':>6} {'refs':>6}")
    for r in results:
        print(
            f"{r['model']:18} {r['latency_ms']:>7} ms {r['prompt_tokens']:>8} "
            f"{r['completion_tokens']:>8} {str(r['valid_json']):>6} {str(r['refs_valid']):>6}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
