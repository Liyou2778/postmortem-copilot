r"""Verify the model endpoint before building on top of it.

Run:
    .\.venv\Scripts\python.exe scripts\check_llm.py

Exit codes: 0 = usable (real or mock), 1 = configured but failing.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Windows consoles default to GBK; force UTF-8 so Chinese output is readable.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - older interpreters
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

from app.config import load_settings  # noqa: E402
from app.llm import LLMConfig, LLMError, ModelClient  # noqa: E402


def main() -> int:
    settings = load_settings()
    print("=" * 62)
    print("模型连通性检查")
    print("=" * 62)
    for k, v in settings.masked().items():
        print(f"  {k:16} = {v}")

    if not settings.llm_configured:
        print()
        print("!! 未配置 LLM_BASE_URL / LLM_MODEL")
        print("   -> 系统会使用确定性 mock 模型（可跑通全链路，但不产生真实推理）")
        print("   -> 填好 .env 后重跑本脚本即可切换真实模型")
        print()
        client = ModelClient(LLMConfig(base_url="", api_key="", model=""))
        t0 = time.time()
        out = client.chat_json("你是根因分析助手。", "Return root cause as JSON.")
        print(f"  mock 调用 OK ({(time.time()-t0)*1000:.0f} ms), keys={list(out.keys())}")
        print()
        print("结论: MOCK 模式可用（可以开始开发，不能上生产）")
        return 0

    client = ModelClient(
        LLMConfig(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout_s=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )
    )
    if client.is_mock:  # defensive
        print("  配置不完整，回落 mock")
        return 1

    print()
    print("正在发送测试请求 ...")
    t0 = time.time()
    try:
        out = client.chat_json(
            "你是一个只输出 JSON 的助手。",
            '返回 {"ok": true, "model_echo": "<你的模型名>"} 这样的 JSON。',
        )
    except LLMError as exc:
        print(f"  FAIL: {exc}")
        print()
        print("排查建议:")
        print("  1) base_url 是否带 /v1（多数厂商需要）")
        print("  2) api_key 是否完整、是否已过期")
        print("  3) model 名称是否与服务商文档一致")
        print("  4) 是否被公司网络拦截（试 curl 该 base_url）")
        return 1

    elapsed = (time.time() - t0) * 1000
    print(f"  真实调用 OK ({elapsed:.0f} ms)")
    print(f"  响应: {out}")
    print()
    print("结论: 真实模型可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
