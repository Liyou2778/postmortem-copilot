# M4 评测报告

- 场景数：3
- 模型：mock
- Recall@1：**0.000**
- Recall@3：**1.000**
- MRR：0.500
- 总耗时：14.5s

## 按故障类型

| 故障类型 | 场景数 | Recall@3 |
|---|---:|---:|
| deploy_regression | 3 | 1.000 |

## 逐场景

| 场景 | 故障 | hit@1 | hit@3 | RR | 时延(s) |
|---|---|---|---|---:|---:|
| S001 | deploy_regression | ❌ | ✅ | 0.5 | 1.88 |
| S002 | deploy_regression | ❌ | ✅ | 0.5 | 1.47 |
| S003 | deploy_regression | ❌ | ✅ | 0.5 | 1.58 |
