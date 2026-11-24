"""Detect which provider a given API key belongs to.

Tries `GET {base_url}/models` against the OpenAI-compatible endpoints of the
common domestic providers and reports which one authenticates. This avoids
guessing the base_url/model pair from the key's shape.

Run: .\.venv\Scripts\python.exe scripts\probe_provider.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

import httpx  # noqa: E402

from app.config import load_settings  # noqa: E402

CANDIDATES: list[tuple[str, str, str]] = [
    # label, base_url, suggested model
    ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("阿里百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("月之暗面", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("硅基流动", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
]


def main() -> int:
    settings = load_settings()
    key = settings.llm_api_key
    if not key:
        print("!! .env 里没有 LLM_API_KEY")
        return 2

    print("=" * 66)
    print("厂商探测（只发 GET /models，不消耗 token）")
    print("=" * 66)
    print(f"  key 指纹: {key[:6]}...{key[-4:]}  (长度 {len(key)})")
    print()

    hits: list[tuple[str, str, str]] = []
    for label, base, model in CANDIDATES:
        url = f"{base}/models"
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(url, headers={"Authorization": f"Bearer {key}"})
            if resp.status_code == 200:
                data = resp.json()
                ids = [m.get("id", "") for m in (data.get("data") or [])][:5]
                print(f"  [OK]   {label:10} {base}")
                if ids:
                    print(f"         可用模型示例: {', '.join(ids)}")
                hits.append((label, base, model))
            else:
                body = resp.text[:80].replace("\n", " ")
                print(f"  [--]   {label:10} HTTP {resp.status_code}  {body}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERR]  {label:10} {type(exc).__name__}: {str(exc)[:60]}")

    print()
    if not hits:
        print("结论: 没有探测到可用厂商。可能原因:")
        print("  1) key 已失效 / 已被撤销")
        print("  2) 该厂商不支持 /models 端点（少数厂商如此）")
        print("  3) 网络被拦截")
        return 1

    label, base, model = hits[0]
    print(f"结论: 命中 {label}")
    print()
    print("把下面两行写进 .env:")
    print(f"  LLM_BASE_URL={base}")
    print(f"  LLM_MODEL={model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
