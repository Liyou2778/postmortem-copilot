r"""Verify the ContextDB knowledge lifecycle over REST.

Goal: prove that we can (1) create a base, (2) upload a text document,
(3) get it ingested, (4) retrieve it semantically. Only then is it safe to
build the memory layer on top.

Run on the ECS: python scripts/probe_ctxdb_kb.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

import httpx

ENV = pathlib.Path("/opt/copilot/.env")


def load_env(path: pathlib.Path) -> dict[str, str]:
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


def jp(r: httpx.Response, n: int = 300) -> str:
    try:
        return json.dumps(r.json(), ensure_ascii=False)[:n]
    except Exception:  # noqa: BLE001
        return r.text[:n]


DOC_NAME = "incident-2026-11-22-order-service"
DOC_TEXT = """# 故障复盘：order-service 支付超时

- 服务：order-service
- 时间窗：30m
- 分析时间：2026-11-22

## 根因
order-service v2.31 将连接池上限从 200 降到 50，导致连接池耗尽，
支付链路请求排队，P99 延迟由 240ms 升至 890ms。

## 证据
- E-CHG-001 变更单 CHG-20261119-031：连接池上限 200 -> 50
- E-TLM-001 指标拐点：P99 240ms -> 890ms
- E-LOG-001 日志出现 connection pool exhausted

## 处置
回滚到 v2.30，连接池恢复 200，延迟回落至 250ms。
"""


def main() -> int:
    env = load_env(ENV)
    key = env.get("CONTEXTDB_API_KEY", "")
    base = env.get("CONTEXTDB_ENDPOINT", "https://context-database.aliyuncs.com").rstrip("/")
    if not key:
        print("CONTEXTDB_API_KEY 未配置")
        return 2
    h = {"Authorization": f"Token {key}", "Content-Type": "application/json"}

    with httpx.Client(timeout=40.0, headers=h) as c:
        # 1) base
        print("[1] 创建知识库 postmortem-history")
        r = c.post(f"{base}/v1/knowledge/knowledge_bases",
                   json={"name": "postmortem-history", "description": "P1 历史复盘归档，用于长期记忆召回"})
        print(f"    HTTP {r.status_code}")
        if r.status_code >= 400:
            # already exists? list and find
            lr = c.get(f"{base}/v1/knowledge/knowledge_bases")
            bases = lr.json().get("knowledge_bases", []) if lr.status_code == 200 else []
            kb = next((b for b in bases if b.get("name") == "postmortem-history"), None)
            if not kb:
                print("    创建失败且找不到同名库:", jp(r))
                return 1
            print(f"    已存在，复用 id={kb['id']}")
        else:
            kb = r.json()
            print(f"    id={kb['id']}  graph_enabled={kb.get('graph_enabled')}")
        kb_id = kb["id"]

        # 2) upload document
        print("\n[2] 上传文档", DOC_NAME)
        r = c.post(f"{base}/v1/knowledge/documents", json={
            "knowledge_base_id": kb_id,
            "name": DOC_NAME + ".md",
            "text": DOC_TEXT,
            "mime_type": "text/markdown",
        })
        print(f"    HTTP {r.status_code}  {jp(r)}")
        if r.status_code >= 400:
            return 1
        doc_id = r.json().get("document_id") or r.json().get("id")
        print(f"    document_id={doc_id}")

        # 3) wait for ingest, then query
        print("\n[3] 等待入库并做语义召回（最多 60 秒）")
        question = "order-service 支付超时的根因是什么？"
        for attempt in range(12):
            time.sleep(5)
            q = c.post(f"{base}/v1/knowledge/query",
                       json={"question": question, "dataset_ids": [kb_id], "top_k": 3})
            if q.status_code != 200:
                print(f"    第{attempt+1}次查询 HTTP {q.status_code} {jp(q)}")
                continue
            data = q.json()
            chunks = data.get("chunks") or []
            print(f"    第{attempt+1}次: chunks={len(chunks)}")
            if chunks:
                print(f"\n    ✓ 召回成功，问题: {question}")
                for i, ch in enumerate(chunks[:3], 1):
                    content = (ch.get("content") or ch.get("text") or "")[:160].replace("\n", " ")
                    score = ch.get("score") or ch.get("similarity") or ""
                    print(f"      [{i}] score={score}  {content}")
                print("\n=== 结论：知识库可作为长期记忆载体 ===")
                return 0
        print("\n    ✗ 60 秒内未召回，可能入库失败或检索配置问题")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
