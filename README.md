# Postmortem Copilot

故障根因分析与复盘生成的多智能体系统（P1）。

> **交付状态（2026-11-24）：M10 核心版本已冻结。**
> 已完成核心链路、评测 3/3、CMDB 租户隔离、K8s 多副本、RBAC/完整 OIDC 联调、
> CI 门禁、OTLP 适配器。**PRD 100% 未完成**，单节点非 HA、OIDC 生产化、
> Network/IO 可观测、真实 OTel Collector 等例外见
> [M10 核心交付验收报告](docs/M10核心交付验收报告.md)。

> 设计文档见上级目录：[P1-技术设计.md](../P1-技术设计.md)
> 场景与依据见：[项目方向确认书.md](../项目方向确认书.md)、[场景依据核查.md](../场景依据核查.md)

---

## 当前进度：M1 公共骨架

| 模块 | 状态 | 验证方式 |
|---|---|---|
| 事件溯源存储 | ✅ 已实现并验证 | append-only，`seq` 单调，重放等价 |
| 状态机与守卫 | ✅ 已实现并验证 | 非法迁移直接拒绝 |
| 租约与围栏 | ✅ 已实现并验证 | 陈旧 worker 写入被拒 |
| 两阶段调用 + 幂等 | ✅ 已实现并验证 | INTENT/RESULT，DONE 复用结果 |
| 崩溃恢复 UNKNOWN 态 | ✅ 已实现并验证 | 悬空调用转人工裁决 |
| HTTP API（FastAPI） | ✅ 已实现并**端到端验证** | 冒烟 18 项全通过 |
| 指标端点 | ✅ 已实现并验证 | `/metrics` 返回 31 行 |
| **M2 Agent 契约** | ✅ 已实现并测试 | 无证据的结论被契约拒绝 |
| **M2 工具层（6 个）** | ✅ 已实现并测试 | 只读/SSRF/限流/脱敏四道闸 |
| **M2 真实 MCP Server** | ✅ **6 个 stdio server 全部连通** | 协议级验证，非本地函数 |
| **M2 MCP 网关** | ✅ 已实现并验证 | 常驻会话 + 同步封装 |
| **M2 DevStack 上游** | ✅ 已实现并验证 | 真实 HTTP，`source=prometheus` 等 |
| **M2 六角色 Agent** | ✅ 已实现并测试 | 每角色只做一件事 |
| **M2 编排器** | ✅ 已实现并测试 | DAG 派发 / 重试 / 死循环熔断 |
| **M2 冲突消解** | ✅ 已实现并测试 | 置信度主导 + 证据合并 |
| **M2 分析链 API** | ✅ 已实现并**端到端验证（真实模型）** | 18s / 4 证据 / 3 结论 / 0 拒绝 |
| **模型接入** | ✅ 已验证真实调用 | DeepSeek `deepseek-flash`，含 mock 兜底 |
| **M3 Saga 补偿** | ✅ 已实现并测试 | 写操作先登记补偿；失败不丢 |
| **M3 租约回收器** | ✅ 已实现并测试 | 区分"可重试"与"副作用不确定" |
| **M3 UNKNOWN 裁决** | ✅ 已实现并测试 | 人工确认后转 SUCCEEDED / FAILED |
| **M3 运维接口** | ✅ 已实现并验证 | `/admin/reaper/run`、`/adjudicate`、`/compensations` |
| **M3 队列 + Worker 进程** | ✅ 已实现并验证 | SQLite / Redis Stream 双实现 |
| **M3 kill -9 演练** | ✅ **通过（4/4 断言）** | 杀 Worker 后任务继续，无重复副作用 |
| **M4 场景库** | ✅ 63 场景（9 类故障 × 7 服务） | 应用层故障注入，无需 K8s |
| **M4 评测流水线** | ✅ 已实现并跑通 | 双侧匹配（证据 + 概念） |
| **M4 指标** | ✅ 63 场景 + 噪声：**R@1=0.603 / R@3=0.857** | 负对照 R@3=0.222，见 `docs/评测方法论.md` |
| **M5 网页界面** | ✅ 已上线 | 首页 / 任务页 / 报告页 / 证据页，服务端渲染 |
| **M5 报告持久化** | ✅ 已实现 | 报告可按 URL 访问，无需重跑 |
| **M5 引用可点击** | ✅ 已实现并验证 | 报告里 `E-XXX-001` 可点开，显示来源/时间/哈希/脱敏级别 |
| **M5 报告下载** | ✅ 已实现 | `/tasks/{id}/report.md` 与 `/report.docx` |
| **M5 Word 导出** | ✅ 已验证 | 37.3 KB，表格/标题保留，**5 个证据超链接**指向证据页 |
| **生产部署** | ✅ **已上线** http://121.41.224.245:8000 | 见 `docs/生产部署记录.md`（公网 IP 可能变化） |
| **M6 容量评估** | ✅ 已完成 | 8 并发分析真并行（系数 1.01）；见 `docs/容量评估报告.md` |
| **M6 压测发现的并发 bug** | ✅ 已修复并加回归测试 | 租约主键从 `step_id` 改为 `(task_id, step_id)` |
| **M7 长期记忆** | ✅ 已上线 | ContextDB 知识库：分析后自动沉淀，下次分析语义召回（0.93–0.97） |
| **M8 真实 Prometheus** | ✅ **2.53 在 ECS 抓取真实指标** | node_exporter 9100，`node` / `prometheus` 两个 target 均 up |
| **M8 服务目录** | ✅ **已上线** | 服务名 → 数据源 / selector / 指标 / real\|mock，查不到就失败关闭 |
| **M8 管理页面** | ✅ 已上线 | `/admin/catalog`、`/admin/datasources`，Basic Auth 保护 |
| **M8 MCP 生产切换** | ✅ **已验证** | `TOOL_BACKEND=mcp`，真实分析 2 证据 / 2 结论 |
| **M8 Mock 治理** | ✅ 已验证 | Mock 只能绑 Mock，报告 / 证据页 / 长期记忆三处强制标黄 |
| **M9 共享 MCP 网关** | ✅ 已上线 | 6 个 server 进程级常驻，健康检查/重启/指标；单次分析 21.8s → 16.4s，4 并发下仍为 6 进程 |
| **M9 真实 Loki 日志** | ✅ 已上线 | Loki 2.9.8 + Promtail 采集 journald，`ecs-copilot` 日志证据 `data_mode=real` |
| **M9 应用指标 + 影响面** | ✅ 已上线 | HTTP/分析/MCP 指标；影响面表带 PromQL，报告可追溯 |
| **M9 真实 Prometheus 评测** | ✅ 已完成 | CPU / 内存 / 磁盘受控演练；M10.1 指标包补齐后 Rank1 3/3、Recall@3 3/3 |
| **M10.1 指标包 + counter** | ✅ 已上线 | 目录支持 gauge/counter + 8 条扩展指标；磁盘 IO 走 `rate()`，修掉磁盘场景 0/3 |
| **M10.2 企业级 CMDB** | ✅ 已上线 | CI 图/关系/时间历史、变更审批状态机、Token RBAC、审计日志、管理 UI、v2 API + v1 MCP 兼容 |
| **M10.2 阿里云发现** | ✅ 已上线 | ECS 实例 RAM 角色 + Resource Center/ActionTrail 只读；15 分钟同步，unowned=0 |
| **M10.3 影响面/Trace/成本** | ✅ 已上线 | 可配置业务口径、OTel 形状 trace、OTLP/HTTP 导出适配器、按 step token/成本；报告含成本章节 |
| **M10.4 通用 DAG + 局部重规划** | ✅ 已上线 | DAG 校验/拓扑执行；失败只跳过下游，独立分支继续；一致性哈希分配 |
| **M10.5 K8s 多副本 + 切流** | ✅ 已上线 | k3d 单节点、Helm、2 副本、滚动更新 30/30 无中断；公网 8000 -> 代理 -> K8s |
| **M10.6 本地认证 + 团队租户** | ✅ 已上线 | 本地账号/API Key、viewer/operator/tenant_admin/platform_admin、任务/报告/OSS/CMDB 租户隔离、DB 限流、同源 CSRF；完整 OIDC 授权码流程已用 Keycloak 联调（dev），可信 header 模式保留 |
| **M10.7 Chaos + 负对照** | ✅ 核心冻结 | Chaos Mesh 2.8.8、PodChaos pod-kill、负对照修复、真实评测 3/3（8/8 real）、评测 CI 门禁；Network/StressChaos 只到可创建/清理，IO 可观测验收未完成，列入 PRD100 例外 |
| docker compose | ✅ 配置完成（含 profiles） | `config` 校验通过；镜像拉取受网络限制，未跑通 |

**已验证的部分**

```bash
# 1) 单元测试：218 个（领域 + 编排 + 可靠性 + 目录/MCP + 网关/影响面 + 指标包 + CMDB/图/审批 + DAG + 认证/租户 + CPU 指标），零外部依赖
python -m unittest discover -s services/core/tests -v

# 2) 端到端冒烟：18 项断言（创建→规划→租约→执行→交付→事件校验）
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1

# 3) 完整分析链（M2）：4 证据 / 2 结论 / 4 工具调用
curl -X POST localhost:8000/api/v1/tasks/<id>/run \
  -H "content-type: application/json" \
  -d '{"service_name":"order-service","window":"30m"}'
```

最近一次结果：**218 / 218 测试通过**（ECS Python 3.11 同样 218 OK / 1 skipped）。
生产实测：`ecs-copilot` 真实 Prometheus + Loki + 企业级 CMDB 分析产出 **8 条真实证据 / 3 条结论**，
含云资源拓扑、变更、Trace、成本与影响面章节；`order-service` devstack 分析产出 5 条 Mock 证据 / 3 条结论，报告首行强制标黄。
真实评测：M10.7 在 K8s/Chaos 环境使用 CPU 利用率 + 磁盘/内存指标包复跑为
**Rank1 3/3、Recall@3 3/3**，证据 **8/8 real**，影响面 3/3 存在。
此前 2/3 的回归过程已记录，最终成绩不是靠调关键词得到。

---

## 核心设计：为什么这样写

### 1. 状态只能来自事件重放

`task.status` 不是权威数据，只是一个读模型。任何一次状态变化都先落 `events` 表，投影函数 `project(events)` 是纯函数。这样做的直接收益是：**审计可以还原"第 37 步是谁在什么时候改的判定"**，而不是只能看到一个被覆盖过的终态。

### 2. 两阶段调用日志

```
INVOCATION_INTENT  →  执行工具  →  INVOCATION_RESULT
```

如果进程死在中间，恢复时扫到"有 INTENT 没 RESULT"，就知道存在**副作用不确定**的调用。这时**不重试**，而是标 UNKNOWN 交人工——因为"建工单"重试一次就是两个工单。

### 3. 租约围栏

```
fence=1  w-old  ──暂停──┐
fence=2  w-new  接管    │
                        └── w-old 恢复后写入 → 被拒（fence 过期）
```

Worker 假死（GC STW、网络抖动）比真死常见。没有围栏，两个 Worker 会同时改同一份状态。

---

## 快速开始

### 方式 A：直接跑（推荐，最快）

```powershell
# 1) 建虚拟环境并装依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r services\core\requirements.txt

# 2) 配置（默认 TOOL_BACKEND=mcp；不填 LLM key 会自动走 mock 模型）
copy .env.example .env
# 编辑 .env：PROMETHEUS_URL / DEVSTACK_URL / LLM_API_KEY / ADMIN_PASSWORD

# 3) 起 Mock 演练上游（order-service 等演示服务需要；9100 被本机占用时换 9101）
.\.venv\Scripts\python.exe -m uvicorn services.devstack.app:app --host 127.0.0.1 --port 9100

# 4) 起服务（另开终端）
.\.venv\Scripts\python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --app-dir .\services\core

# 5) 打开首页 / 服务目录（设了 ADMIN_PASSWORD 会弹 Basic Auth）
#    http://127.0.0.1:8000/
#    http://127.0.0.1:8000/admin/catalog

# 6) 冒烟
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1
```

本机想连 ECS 上的真实 Prometheus：

```powershell
# 另开一个终端，把本机 9090 转到 ECS 的 127.0.0.1:9090
ssh -N -L 9090:127.0.0.1:9090 root@121.41.224.245
# .env 保持 PROMETHEUS_URL=http://127.0.0.1:9090
```

> ECS 上 node_exporter 占用了 9100，devstack 必须换 9101；本机可以保持 9100。

### 方式 B：容器（需要依赖服务时再启用）

```powershell
docker compose up -d                                    # core + redis
docker compose --profile db up -d                       # 追加 MySQL(3307)
docker compose --profile obs up -d                      # 追加 Prometheus + Grafana
```

> 国内 Docker Hub 拉取可能很慢。镜像拉不下来时用方式 A，功能完全一致。

### 手动走一遍

```powershell
$TASK = (Invoke-RestMethod -Method Post -Uri localhost:8000/api/v1/tasks `
  -ContentType 'application/json' -Body '{"title":"P1 支付超时故障复盘"}').task_id

Invoke-RestMethod -Method Post -Uri "localhost:8000/api/v1/tasks/$TASK/plan" `
  -ContentType 'application/json' `
  -Body '{"steps":[{"step_id":"s1","role":"telemetry"},{"step_id":"s2","role":"log"}]}'

Invoke-RestMethod -Method Post -Uri "localhost:8000/api/v1/tasks/$TASK/start"
Invoke-RestMethod -Method Post -Uri "localhost:8000/api/v1/tasks/$TASK/steps/s1/lease" `
  -ContentType 'application/json' -Body '{"owner":"w1"}'
```

---

## 目录结构

```
services/core/
  app/
    events.py         事件定义、幂等键
    store.py          append-only 存储 + 幂等表（SQLite / MySQL 同构）
    statemachine.py   状态机守卫 + 投影
    lease.py          租约与围栏
    service.py        唯一允许写事件的入口
    api.py            FastAPI 适配层
    catalog.py        服务目录 / 数据源绑定 / 解析（DB 层）
    catalog_router.py 服务 → 数据源路由 + real|mock 注入
    catalog_api.py    /admin 管理页 + /api/v1/catalog JSON
  tests/              218 个 stdlib unittest
services/mcp_servers/ 6 个 stdio MCP server（prometheus/loki/cmdb/deploy/ticket/kb）
services/cmdb/        企业级 CMDB：CI 图 / 审批 / 审计 / RBAC / 阿里云发现（127.0.0.1:9200）
services/devstack/    故障注入 Mock 上游（9100/9101）
docs/服务目录与真实数据接入.md
docs/CMDB企业级设计与落地.md
docs/M9-共享网关与真实评测.md
docs/M10-指标包与PRD100计划.md
docs/K8s部署与切流.md
docs/认证与多租户.md
docs/Chaos与负对照.md
docs/真实Prometheus评测.md
docs/OTel导出.md
docs/PRD100验收矩阵.md
docs/M10核心交付验收报告.md
migrations/           001–005（init / mysql / catalog / metric pack / impact policy）
deploy/prometheus.yml
docker-compose.yml
```

---

## 核心冻结后的 PRD 100% 例外

1. OIDC 生产化：Keycloak 当前 dev/HTTP/H2，需要 HTTPS、持久化、备份、轮换；
2. 跨节点 K8s HA（当前单节点 k3d）；
3. Network/IO Chaos 可观测验收；
4. 真实 OTel Collector/看板/告警；
5. 远端 GitHub Actions 实际运行；
6. 表单级 CSRF。
7. 影响面业务系数（财务口径）仍未配置。

冻结口径：以上未完成前，只称“M10 核心完成”，不称 PRD 100%。
