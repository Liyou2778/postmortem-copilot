# M4 评测报告

- 场景数：14
- 模型：real
- Recall@1：**0.714**
- Recall@3：**0.714**
- MRR：0.714
- 总耗时：329.5s

## 按故障类型

| 故障类型 | 场景数 | Recall@3 |
|---|---:|---:|
| resource_saturation | 7 | 0.429 |
| traffic_spike | 7 | 1.000 |

## 逐场景

| 场景 | 故障 | hit@1 | hit@3 | RR | 时延(s) |
|---|---|---|---|---:|---:|
| S008 | resource_saturation | ✅ | ✅ | 1.0 | 23.01 |
| S009 | resource_saturation | ❌ | ❌ | 0.0 | 25.98 |
| S010 | resource_saturation | ❌ | ❌ | 0.0 | 45.38 |
| S011 | resource_saturation | ❌ | ❌ | 0.0 | 19.09 |
| S012 | resource_saturation | ✅ | ✅ | 1.0 | 22.83 |
| S013 | resource_saturation | ✅ | ✅ | 1.0 | 19.63 |
| S014 | resource_saturation | ❌ | ❌ | 0.0 | 24.36 |
| S057 | traffic_spike | ✅ | ✅ | 1.0 | 18.92 |
| S058 | traffic_spike | ✅ | ✅ | 1.0 | 18.51 |
| S059 | traffic_spike | ✅ | ✅ | 1.0 | 19.21 |
| S060 | traffic_spike | ✅ | ✅ | 1.0 | 16.5 |
| S061 | traffic_spike | ✅ | ✅ | 1.0 | 18.25 |
| S062 | traffic_spike | ✅ | ✅ | 1.0 | 21.17 |
| S063 | traffic_spike | ✅ | ✅ | 1.0 | 22.99 |
