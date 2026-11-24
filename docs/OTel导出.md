# OTel Trace 导出

> 状态：投影 + OTLP/HTTP 导出适配器已完成；已用本地 mock receiver
> 完成端到端导出验证（200 / 6 spans）；验证后已停止 mock 并清空生产
> endpoint，未在真实 Collector 上压测。

## 1. 已有能力

Trace 不单独存库，直接由事件日志投影：

```text
task / step / tool span
trace_id = sha256(task_id)[:32]
span_id  = sha256(...)[:16]
start/end 纳秒时间戳
status OK/ERROR/UNSET
attributes: role, owner, fence, attempt, tool, step_id, idempotency_key
```

查询：

```http
GET /api/v1/tasks/{task_id}/trace
```

## 2. OTLP/HTTP 导出

```dotenv
OTEL_EXPORTER_ENDPOINT=http://otel-collector:4318
OTEL_SERVICE_NAME=postmortem-copilot
```

手动导出：

```http
POST /api/v1/tasks/{task_id}/trace/export
```

返回：

```json
{"ok": true, "status": 200, "error": ""}
```

约束：

- 导出失败不会影响分析/报告；
- 不引入 OTel SDK，使用 OTLP/HTTP JSON，减少镜像依赖；
- `OTEL_EXPORTER_ENDPOINT` 为空时明确返回未配置；
- 没有真实 collector 的端到端验证，只有单元测试和本地 mock。

## 3. 测试

```text
test_projection_has_otlp_shape
test_missing_endpoint_is_not_an_exception
test_successful_post
```
