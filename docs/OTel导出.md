# OTel Trace 导出

> 状态：投影 + OTLP/HTTP 导出适配器 + **真实 OTel Collector + Jaeger**
> 已完成端到端验证；Jaeger UI 可查询到 `postmortem-copilot` trace。

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
- 当前部署：

```text
OTel Collector：otel/opentelemetry-collector-contrib:0.110.0
  host :4319 -> container :4318
Jaeger：jaegertracing/all-in-one:1.60
  UI :16686
  OTLP :4317
应用 OTEL_EXPORTER_ENDPOINT=http://172.24.19.160:4319
```

验证：

```text
trace/export: {"ok": true}
Collector debug exporter: resource spans 1, spans 6
Jaeger services: ["postmortem-copilot","jaeger-all-in-one"]
```

仍未做：Grafana 看板、告警规则、Collector 高可用/持久化。

## 3. 测试

```text
test_projection_has_otlp_shape
test_missing_endpoint_is_not_an_exception
test_successful_post
```
