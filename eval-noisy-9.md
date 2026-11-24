# M4 评测报告

- 场景数：9
- 模型：real
- Recall@1：**0.889**
- Recall@3：**1.000**
- MRR：0.926
- 总耗时：202.9s

## 按故障类型

| 故障类型 | 场景数 | Recall@3 |
|---|---:|---:|
| config_drift | 1 | 1.000 |
| dependency_timeout | 1 | 1.000 |
| deploy_regression | 1 | 1.000 |
| dns_error | 1 | 1.000 |
| io_stall | 1 | 1.000 |
| network_partition | 1 | 1.000 |
| pod_kill | 1 | 1.000 |
| resource_saturation | 1 | 1.000 |
| traffic_spike | 1 | 1.000 |

## 逐场景

| 场景 | 故障 | hit@1 | hit@3 | RR | 时延(s) |
|---|---|---|---|---:|---:|
| S001 | deploy_regression | ✅ | ✅ | 1.0 | 23.6 |
| S002 | resource_saturation | ✅ | ✅ | 1.0 | 19.6 |
| S003 | dependency_timeout | ✅ | ✅ | 1.0 | 17.96 |
| S004 | config_drift | ✅ | ✅ | 1.0 | 18.02 |
| S005 | pod_kill | ✅ | ✅ | 1.0 | 20.3 |
| S006 | network_partition | ✅ | ✅ | 1.0 | 16.58 |
| S007 | dns_error | ❌ | ✅ | 0.333 | 24.12 |
| S008 | io_stall | ✅ | ✅ | 1.0 | 21.05 |
| S009 | traffic_spike | ✅ | ✅ | 1.0 | 30.15 |
