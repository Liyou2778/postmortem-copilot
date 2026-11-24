# M4 评测报告

- 场景数：63
- 模型：real
- Recall@1：**0.603**
- Recall@3：**0.857**
- MRR：0.714
- 总耗时：1277.6s

## 按故障类型

| 故障类型 | 场景数 | Recall@3 |
|---|---:|---:|
| config_drift | 7 | 1.000 |
| dependency_timeout | 7 | 1.000 |
| deploy_regression | 7 | 1.000 |
| dns_error | 7 | 1.000 |
| io_stall | 7 | 1.000 |
| network_partition | 7 | 0.857 |
| pod_kill | 7 | 0.714 |
| resource_saturation | 7 | 0.714 |
| traffic_spike | 7 | 0.429 |

## 逐场景

| 场景 | 故障 | hit@1 | hit@3 | RR | 时延(s) |
|---|---|---|---|---:|---:|
| S001 | deploy_regression | ✅ | ✅ | 1.0 | 13.03 |
| S002 | deploy_regression | ✅ | ✅ | 1.0 | 17.7 |
| S003 | deploy_regression | ✅ | ✅ | 1.0 | 22.13 |
| S004 | deploy_regression | ✅ | ✅ | 1.0 | 26.55 |
| S005 | deploy_regression | ✅ | ✅ | 1.0 | 20.95 |
| S006 | deploy_regression | ❌ | ✅ | 0.5 | 38.55 |
| S007 | deploy_regression | ✅ | ✅ | 1.0 | 21.18 |
| S008 | resource_saturation | ✅ | ✅ | 1.0 | 15.63 |
| S009 | resource_saturation | ❌ | ✅ | 0.333 | 21.29 |
| S010 | resource_saturation | ❌ | ✅ | 0.333 | 17.07 |
| S011 | resource_saturation | ❌ | ❌ | 0.0 | 21.91 |
| S012 | resource_saturation | ❌ | ✅ | 0.333 | 18.44 |
| S013 | resource_saturation | ❌ | ✅ | 0.5 | 20.17 |
| S014 | resource_saturation | ❌ | ❌ | 0.0 | 16.72 |
| S015 | dependency_timeout | ✅ | ✅ | 1.0 | 13.74 |
| S016 | dependency_timeout | ✅ | ✅ | 1.0 | 16.38 |
| S017 | dependency_timeout | ✅ | ✅ | 1.0 | 17.94 |
| S018 | dependency_timeout | ✅ | ✅ | 1.0 | 17.48 |
| S019 | dependency_timeout | ✅ | ✅ | 1.0 | 21.5 |
| S020 | dependency_timeout | ✅ | ✅ | 1.0 | 26.3 |
| S021 | dependency_timeout | ✅ | ✅ | 1.0 | 15.87 |
| S022 | config_drift | ✅ | ✅ | 1.0 | 19.45 |
| S023 | config_drift | ✅ | ✅ | 1.0 | 23.6 |
| S024 | config_drift | ✅ | ✅ | 1.0 | 14.32 |
| S025 | config_drift | ✅ | ✅ | 1.0 | 19.47 |
| S026 | config_drift | ✅ | ✅ | 1.0 | 20.77 |
| S027 | config_drift | ✅ | ✅ | 1.0 | 15.55 |
| S028 | config_drift | ❌ | ✅ | 0.5 | 16.18 |
| S029 | pod_kill | ✅ | ✅ | 1.0 | 17.0 |
| S030 | pod_kill | ❌ | ✅ | 0.333 | 21.44 |
| S031 | pod_kill | ❌ | ❌ | 0.0 | 19.89 |
| S032 | pod_kill | ❌ | ✅ | 0.333 | 21.03 |
| S033 | pod_kill | ✅ | ✅ | 1.0 | 21.54 |
| S034 | pod_kill | ❌ | ✅ | 0.5 | 22.86 |
| S035 | pod_kill | ❌ | ❌ | 0.0 | 21.23 |
| S036 | network_partition | ✅ | ✅ | 1.0 | 18.76 |
| S037 | network_partition | ❌ | ❌ | 0.0 | 15.39 |
| S038 | network_partition | ✅ | ✅ | 1.0 | 16.69 |
| S039 | network_partition | ✅ | ✅ | 1.0 | 23.65 |
| S040 | network_partition | ✅ | ✅ | 1.0 | 21.43 |
| S041 | network_partition | ✅ | ✅ | 1.0 | 17.3 |
| S042 | network_partition | ❌ | ✅ | 0.5 | 28.72 |
| S043 | dns_error | ✅ | ✅ | 1.0 | 13.48 |
| S044 | dns_error | ❌ | ✅ | 0.5 | 18.16 |
| S045 | dns_error | ❌ | ✅ | 0.5 | 14.79 |
| S046 | dns_error | ✅ | ✅ | 1.0 | 19.33 |
| S047 | dns_error | ✅ | ✅ | 1.0 | 16.89 |
| S048 | dns_error | ✅ | ✅ | 1.0 | 17.98 |
| S049 | dns_error | ❌ | ✅ | 0.5 | 16.05 |
| S050 | io_stall | ✅ | ✅ | 1.0 | 40.94 |
| S051 | io_stall | ✅ | ✅ | 1.0 | 20.33 |
| S052 | io_stall | ✅ | ✅ | 1.0 | 16.84 |
| S053 | io_stall | ✅ | ✅ | 1.0 | 16.55 |
| S054 | io_stall | ✅ | ✅ | 1.0 | 21.02 |
| S055 | io_stall | ❌ | ✅ | 0.5 | 15.41 |
| S056 | io_stall | ✅ | ✅ | 1.0 | 17.45 |
| S057 | traffic_spike | ❌ | ❌ | 0.0 | 17.16 |
| S058 | traffic_spike | ❌ | ✅ | 0.333 | 22.85 |
| S059 | traffic_spike | ❌ | ❌ | 0.0 | 18.67 |
| S060 | traffic_spike | ✅ | ✅ | 1.0 | 16.66 |
| S061 | traffic_spike | ❌ | ✅ | 0.5 | 23.16 |
| S062 | traffic_spike | ❌ | ❌ | 0.0 | 24.75 |
| S063 | traffic_spike | ❌ | ❌ | 0.0 | 19.65 |
