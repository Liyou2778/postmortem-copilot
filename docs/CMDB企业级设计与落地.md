# 企业级 CMDB 设计与落地

> 项目：P1 Postmortem Copilot
> 上线：2026-11-23
> 服务：`services/cmdb`（FastAPI + MySQL，`127.0.0.1:9200`）
> 管理页：`/admin/cmdb`（核心应用 Basic Auth 保护）

---

## 1. 为什么不是“服务表 + JSON 上下游”

P1 早期版本用 `cmdb_service` 存服务，上下游是 JSON 数组。它足以做演示，但不够企业级：

- 没有类型化 CI（service/host/middleware/database/storage）；
- 没有关系实体、关系关键度、来源；
- 没有时间维度（谁在什么时候改了拓扑）；
- 没有变更单/审批/审计；
- 没有发现与人工维护的边界。

M10.2 把它拆成两层：

```text
v1 兼容层：/api/v1/topology、/api/v1/changes
              （MCP cmdb/deploy 不需要改）
v2 控制面：/api/v2/cis、relations、impact、changes、audit、quality
              （企业级数据模型/审批/RBAC）
```

---

## 2. 数据模型

| 表 | 作用 |
|---|---|
| `ci` | 当前 CI：类型、owner、tier、env、状态、属性、版本、来源 |
| `ci_history` | 每次 CI 更新前的快照 + `valid_from/valid_to`，可回溯任意时点 |
| `ci_relation` | 有向关系：`depends_on / runs_on / stores_in / calls / routes_to / realized_by` |
| `ci_relation_history` | 关系变更快照 |
| `change_request` | 变更单：服务、风险、状态机、计划/执行时间、回滚关联 |
| `change_approval` | 审批记录：approver、decision、comment、时间 |
| `audit_log` | 所有写操作：actor、role、action、对象、before/after、时间 |

### 逻辑依赖 vs 物理资源

阿里云发现出来的 RDS/OSS 是**物理资源**；人工维护的 `rds-mysql`/`oss` 是**逻辑依赖**。

```text
ecs-copilot
  ├── depends_on   rds-mysql
  │       └── realized_by  aliyun-database-rm-...
  ├── depends_on   oss
  │       └── realized_by  aliyun-storage-copilot-artifacts-ly
  └── runs_on      aliyun-host-i-bp...
```

这样业务拓扑稳定，云资源可以替换/漂移，而不会把每次发现都变成一条新的服务依赖。

---

## 3. 控制面 API

所有 `/api/v2/*` 需要 `X-CMDB-Token`，角色从高到低：

```text
approver > editor > viewer
```

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/api/v2/cis` | viewer | CI 列表 |
| GET | `/api/v2/cis/{id}` | viewer | CI 详情 |
| PUT | `/api/v2/cis/{id}` | editor | 新建/更新 CI |
| GET | `/api/v2/cis/{id}/relations` | viewer | 关系 |
| PUT | `/api/v2/cis/{from}/relations/{to}` | editor | 新建/更新关系 |
| GET | `/api/v2/impact/{id}` | viewer | 多跳影响面 |
| GET | `/api/v2/quality` | viewer | 数据质量 |
| GET/POST | `/api/v2/changes` | viewer/editor | 变更单 |
| POST | `/api/v2/changes/{id}/submit` | editor | 提交审批 |
| POST | `/api/v2/changes/{id}/approve` | approver | 批准/驳回 |
| POST | `/api/v2/changes/{id}/execute` | editor | 执行结果 |
| POST | `/api/v2/changes/{id}/rollback` | editor | 回滚登记 |
| GET | `/api/v2/audit` | approver | 审计日志 |

---

## 4. 变更状态机

```text
draft
  └── submit ──> pending_approval
                    ├── approve ──> approved
                    └── reject  ──> rejected
approved
  └── execute ──> succeeded / failed
succeeded|failed
  └── rollback ──> rolled_back
```

所有 transition 都写 `audit_log`；非法 transition 返回 400，不会静默更新。

---

## 5. 阿里云发现

### 凭据

- ECS 实例 RAM 角色：`copilot-cmdb-reader`
- 信任策略只允许 `ecs.aliyuncs.com`
- 权限：Resource Center / Config / ActionTrail / ECS / RDS / OSS 只读
- 不落 AccessKey；SDK 通过实例元数据取临时凭据
- `copilot-oss` 的 AK 完全不动，只服务 OSS 报告上传

### 数据源

| 来源 | 用途 |
|---|---|
| Resource Center `SearchResources` | ECS / RDS / OSS / SLB 等资源清单与标签 |
| ActionTrail `LookupEvents` | 真实写操作变更事件 |
| Config `ListDiscoveredResources` | 预留：配置清单/变更时间线 |
| 标签 `service/owner/tier/env` | 资源 → 服务归属、责任人、等级 |

### 归属规则

- 有 `service` 标签：挂到该服务的 CI；
- ECS 默认宿主机（`CMDB_DEFAULT_HOST_ID`）：挂到 `ecs-copilot`；
- 逻辑 CI 唯一匹配（`rds-mysql`/`oss`）：建 `realized_by`，不重复挂服务；
- 没有 `service` 但有人工 owner：`unattached=true`，只做 inventory；
- 没有 owner：`unowned=true`，质量页标红。

### 调度

`copilot-cmdb-sync.timer` 每 15 分钟执行：

```bash
python scripts/sync_aliyun_cmdb.py --apply --changes --hours 2
```

失败只影响同步，不影响 CMDB 读路径；systemd 会记录失败。

---

## 6. 安全边界

| 层 | 措施 |
|---|---|
| CMDB 服务 | 只监听 `127.0.0.1:9200`，安全组不开放 |
| v2 API | 角色 Token；写操作审计 |
| 管理页 | 核心应用 Basic Auth；浏览器不接触 CMDB Token |
| 阿里云 | ECS 实例角色 + 最小只读策略；不落 AK |
| 变更 | editor/approver 分离，审批走状态机 |

### 多租户（M10.7）

- `ci` / `ci_relation` / `change_request` 增加 `tenant_id`，默认 `team-core`；
- v2 Token 默认绑定 `CMDB_TENANT`，只能读写本租户；
- `CMDB_TOKEN_PLATFORM` 可选，平台 token 可跨租户；
- CI/关系/影响面/变更列表按 tenant 过滤；
- 审计日志保持平台级，供 `platform_admin` 审查；
- v1 MCP 兼容接口仍按平台级只读使用，没有向外部暴露。

未做：表单级 CSRF token、完整 OIDC。

---

## 7. 生产验证

```text
CMDB 服务：active
CI：11（tenant=team-core）
ci_relation / change_request 已加 tenant_id
租户计数：team-core=11
v2 viewer token：200，返回 11 条（仅 team-core）
关系：8
阿里云资源：ECS 1 / RDS 1 / OSS 2
数据质量：unowned=0, unattached=1（ActionTrail 审计桶，正常）
ActionTrail：copilot-cmdb-trail / Enable / All / OSS
分析链路：8/8 证据 real，工具调用全部 ok
测试：218 / 218
```

管理页：

```text
http://121.41.224.245:8000/admin/cmdb
```

匿名 401，登录后 200；展示 CI、质量、变更审批、审计。

---

## 8. 已知边界

- 阿里云发现目前覆盖 ECS/RDS/OSS/SLB/Redis，类型可扩展；
- Config 时间线尚未接入（预留接口）；
- 单账号，不做资源目录多账号；
- 单实例 CMDB，未做多副本/RDS 读写分离；
- 管理 UI 目前以查看和审批为主，完整 CI 编辑器后续补；
- ActionTrail 已投递 OSS，SLS 长期审计未开。

这些边界都显式暴露，不用 mock 数据或“看起来全绿”掩盖。
