r"""Dump raw model output per agent, to see exactly how the prompt is misread.

Run: .\.venv\Scripts\python.exe scripts\debug_agent.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

from app.agents import (  # noqa: E402
    AgentContext,
    ChangeAgent,
    LogAgent,
    TelemetryAgent,
)
from app.config import load_settings  # noqa: E402
from app.llm import ModelClient  # noqa: E402
from app.tools import build_default_registry  # noqa: E402


def main() -> int:
    settings = load_settings()
    print(f"model = {settings.llm_model} @ {settings.llm_base_url}")
    llm = ModelClient()
    print(f"is_mock = {llm.is_mock}")
    print()

    captured: list[tuple[str, dict]] = []
    original = llm.chat_json

    def spy(system: str, user: str) -> dict:
        result = original(system, user)
        captured.append(("call", result))
        return result

    llm.chat_json = spy  # type: ignore[method-assign]

    ctx = AgentContext(
        task_id="debug",
        service="order-service",
        window="30m",
        tools=build_default_registry(settings),
        llm=llm,
        settings=settings,
    )

    for name, agent in (("telemetry", TelemetryAgent()), ("log", LogAgent()), ("change", ChangeAgent())):
        before = len(captured)
        try:
            out = agent.run(ctx)
        except Exception as exc:  # noqa: BLE001
            print(f"### {name}: EXCEPTION {type(exc).__name__}: {exc}")
            continue
        print(f"### {name}")
        print(f"  claims kept : {len(out.claims)}")
        for claim in out.claims:
            print(f"    - {claim.claim_id} cred={claim.confidence} refs={claim.evidence_refs}")
        if captured[before:]:
            raw = captured[-1][1]
            print("  raw keys    :", list(raw.keys()))
            print("  raw (前 700 字):")
            print("   ", json.dumps(raw, ensure_ascii=False)[:700].replace("\n", " "))
        print()

    print("### rejections")
    for r in ctx.rejections:
        print("  -", str(r)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
