# 真实 Prometheus 评测（M9）

> 场景：ECS 本机受控故障；分析链：真实 Prometheus + Loki + MCP。
> 样本量小，仅用于验证链路与召回方向，不作统计显著性结论。

- 场景数：3
- Rank1 命中：3/3
- Recall@3：3/3

| 场景 | 状态 | 结论数 | Rank1 | Recall@3 | 真实证据 | 影响面 | 主要结论 |
|---|---|---|---|---|---|---|---|
| cpu | REVIEWING | 3 | ✅ | ✅ | 8/8 | ✅ | node_load1 峰值 11.48（基线 0.00），指向节点 CPU/运行队列饱和 |
| memory | REVIEWING | 3 | ✅ | ✅ | 8/8 | ✅ | 节点可用内存从约 1.2GB 骤降至 150MB（-87.5%），表明内存耗尽是最可能的根因，疑似触发 OOM 杀死进程。 |
| disk | REVIEWING | 3 | ✅ | ✅ | 8/8 | ✅ | 磁盘 IO指标 node_disk_io_time_seconds_total 相对基线变化 12202.8%，是窗口内最显著且超出主指标的异常，指向磁盘 IO饱和 |

## 逐场景明细

### cpu

- task: `dd9ba672-4532-4372-888d-22406fbcf5a8`
- 耗时: 22.73s
- 关键词: cpu, 运行队列, 资源饱和, 占用, saturat
- 影响面: `{"requests_incident": 0.3556, "requests_baseline": 1.6002, "error_rate_incident": 0.0, "error_rate_baseline": 0.0, "p95_incident": 45.0, "p95_baseline": 24.475, "targets_up_incident": 2.0, "targets_up_baseline": 2.0}`

- ✅ rank1: node_load1 峰值 11.48（基线 0.00），指向节点 CPU/运行队列饱和
- ✅ rank2: 节点 CPU 使用率在窗口内由 84.9% 升至 100% 并持续饱和，与 node_load1 同步维持 10~11 的高位；CPU 资源耗尽最可能是本次负载偏高的根因
- ❌ rank3: 磁盘 IO 时间在窗口内上升 338.4%，是唯一显著异常的指标，最可疑的根因方向是磁盘 IO 压力

### memory

- task: `01200c5b-ac4d-49da-b81f-bcc0bb0fe8be`
- 耗时: 20.4s
- 关键词: 内存, memory, oom, 可用内存
- 影响面: `{"requests_incident": 0.3557, "requests_baseline": 0.3777, "error_rate_incident": 0.0, "error_rate_baseline": 0.0, "p95_incident": 24.175, "p95_baseline": 45.0, "targets_up_incident": 2.0, "targets_up_baseline": 2.0}`

- ✅ rank1: 节点可用内存从约 1.2GB 骤降至 150MB（-87.5%），表明内存耗尽是最可能的根因，疑似触发 OOM 杀死进程。
- ❌ rank2: 日志查询返回 0 条匹配记录，未出现 error/exception/timeout/refused 等失败特征，日志侧不支持应用层异常作为根因
- ✅ rank3: node_load1 从基线 10.85 降至 5.42（-50%），与进程被终止后负载骤降相符，是内存耗尽的后果而非独立故障。

### disk

- task: `4dc5b529-c1b4-492d-8ac9-ec6c9ed1f31e`
- 耗时: 23.41s
- 关键词: 磁盘, disk, io, iowait, 写入, 读写
- 影响面: `{"requests_incident": 0.3778, "requests_baseline": 0.3556, "error_rate_incident": 0.0, "error_rate_baseline": 0.0, "p95_incident": 23.0134, "p95_baseline": 45.0, "targets_up_incident": 2.0, "targets_up_baseline": 2.0}`

- ✅ rank1: 磁盘 IO指标 node_disk_io_time_seconds_total 相对基线变化 12202.8%，是窗口内最显著且超出主指标的异常，指向磁盘 IO饱和
- ❌ rank2: node_memory_MemAvailable_bytes 在 16:43:56~16:44:56 期间从约 1.15GB 骤降至约 144MB（-87.5%），说明该时段内存被快速耗尽，是本轮异常的最上游诱因。
- ❌ rank3: node_load1 从基线 11.075 降至 2.0（-81.9%），与 CPU 使用率从约 100% 跌至 13.9%（-86.1%）同步发生，说明工作负载在高内存压力下被终止或重启，load/CPU 下降是故障结果而非性能改善。
