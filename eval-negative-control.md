# M4 评测报告

- 场景数：9
- 模型：mock
- Recall@1：**0.111**
- Recall@3：**0.222**
- MRR：0.167
- 总耗时：27.3s

## 按故障类型

| 故障类型 | 场景数 | Recall@3 |
|---|---:|---:|
| config_drift | 1 | 0.000 |
| dependency_timeout | 1 | 0.000 |
| deploy_regression | 1 | 0.000 |
| dns_error | 1 | 0.000 |
| io_stall | 1 | 0.000 |
| network_partition | 1 | 1.000 |
| pod_kill | 1 | 1.000 |
| resource_saturation | 1 | 0.000 |
| traffic_spike | 1 | 0.000 |

## 逐场景

| 场景 | 故障 | hit@1 | hit@3 | RR | 时延(s) |
|---|---|---|---|---:|---:|
| S001 | deploy_regression | ❌ | ❌ | 0.0 | 2.06 |
| S002 | resource_saturation | ❌ | ❌ | 0.0 | 1.7 |
| S003 | dependency_timeout | ❌ | ❌ | 0.0 | 1.78 |
| S004 | config_drift | ❌ | ❌ | 0.0 | 1.68 |
| S005 | pod_kill | ❌ | ✅ | 0.5 | 1.69 |
| S006 | network_partition | ✅ | ✅ | 1.0 | 1.73 |
| S007 | dns_error | ❌ | ❌ | 0.0 | 1.71 |
| S008 | io_stall | ❌ | ❌ | 0.0 | 1.7 |
| S009 | traffic_spike | ❌ | ❌ | 0.0 | 1.68 |
