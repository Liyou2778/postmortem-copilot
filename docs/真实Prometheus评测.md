# 真实 Prometheus 评测（M9 → M10.1）

> 项目：P1 Postmortem Copilot
> 时间：2026-11-22 23:27 CST
> 脚本：`scripts/real_eval.py`
> 原始结果：[eval-real-m9.md](../eval-real-m9.md) / [eval-real-m9.json](../eval-real-m9.json)

---

## 1. 为什么单独做这一套

已有 63 个 devstack 场景，优点是确定、可进 CI；缺点是故障信号是“造”出来的。
真实评测回答另一个问题：

> 在真实 Prometheus + 真实 Loki + 真实 MCP 链路上，模型能不能把真实主机故障排到 Top3？

因此两套评测并存，不互相替代。

---

## 2. 方法

每个场景在 ECS 本机执行：

1. 注入受控故障（`Fault` 进程组，带唯一 marker，跑完强制 `killpg`）；
2. 等 45s，让 Prometheus 至少抓到 3 个采样点；
3. 调真实分析链：`POST /tasks/{id}/run`，`service=ecs-copilot`，窗口 `3m`；
4. 按关键词对 Top1 / Top3 打分：
   - CPU：`cpu / load / 负载 / 资源饱和 / 占用`
   - 内存：`内存 / memory / oom / 可用内存`
   - 磁盘：`磁盘 / disk / io / iowait / 写入 / 读写`
5. 同时检查每条结论是否引用真实证据、报告是否有影响面。

> 关键词匹配使用 ASCII 词边界。第一版用朴素的 `in`，把 `exception` 里的
> "io" 误判成磁盘命中；这个假阳性已修复，并重算了结果（不重跑分析）。

受控故障：

| 场景 | 注入 | 命令/进程 |
|---|---|---|
| CPU | 2 个 `yes` 占满 2 vCPU | `timeout 180 yes` |
| 内存 | 1.5 GB 常驻并 touch 全部页 | Python `bytearray` + 逐页写 |
| 磁盘 | 循环写 512 MB 并 `fdatasync` | `dd of=/tmp/m9-disk-io.bin` |

每次故障跑完都验证没有残留进程；第一版曾把 `yes` 泄漏到下一个场景，已加
`start_new_session=True` + `killpg` + 预清理 marker。

---

## 3. 结果（M10.7 在 K8s/Chaos 环境复跑，当前代码）

| 场景 | 状态 | 结论数 | Rank1 | Recall@3 | 真实证据 | 影响面 | 主要结论 |
|---|---|---|---|---|---|---|---|
| CPU | REVIEWING | 3 | ✅ | ✅ | 8/8 | ✅ | node_load1 峰值与 CPU 利用率饱和，指向 CPU/运行队列压力 |
| 内存 | REVIEWING | 3 | ✅ | ✅ | 8/8 | ✅ | 可用内存骤降至约 150MB，指向真实内存压力 |
| 磁盘 | REVIEWING | 3 | ✅ | ✅ | 8/8 | ✅ | 磁盘 IO 显著异常，确定性候选指向磁盘 IO 饱和 |

**M10.7 最终复跑：Rank1 = 3/3，Recall@3 = 3/3。**

> 中间曾出现 2/3、0/1 的回归；最终通过新增
> `node_cpu_seconds_total -> 100*(1-avg(rate(...{mode="idle"})))` 的真实
> CPU 利用率指标，而不是继续调关键词，修复 CPU 场景。回归过程保留在
> [Chaos 与负对照](Chaos与负对照.md) 中。

逐场景任务：

- CPU：`dd9ba672-4532-4372-888d-22406fbcf5a8`
- 内存：`01200c5b-ac4d-49da-b81f-bcc0bb0fe8be`
- 磁盘：`4dc5b529-c1b4-492d-8ac9-ec6c9ed1f31e`

证据 8/8 为 `data_mode=real`；影响面 3/3 均存在。

---

## 4. 读结论：磁盘为什么从 0/3 变成 3/3

M9 时磁盘没命中，不是“模型不会说磁盘”，而是**输入里没有磁盘信号**：

- `ecs-copilot` 的目录绑定只有：
  - 主指标 `node_load1`
  - 辅助指标 `node_memory_MemAvailable_bytes`
- 磁盘写入抬高的是 `node_disk_io_time_seconds_total` /
  `node_disk_write_time_seconds_total` 这类 **counter**，既不在绑定里，也不能
  直接当 gauge 做异常检测。

M10.1 修的是输入，不是评测关键词：

1. 目录绑定增加 `extra_metrics`（最多 8 条，JSON）；
2. 每个指标声明 `gauge | counter`，counter 查询自动包 `rate(...[窗口/2])`；
3. `ecs-copilot` 增加 `node_disk_io_time_seconds_total`（counter）；
4. Telemetry 把扩展指标作为独立证据写入，并在“扩展信号变化 ≥ 100% 且 ≥ 主指标
   变化的 5 倍”时生成确定性根因候选——避免 CPU 压力顺带抬高磁盘 IO 时误判为
   磁盘根因。

结果：磁盘场景 Rank1 由 ❌ 变 ✅；CPU/内存不受额外干扰（5 倍门槛）。

### 下一步

1. 增加负对照（无故障）检查误报；
2. 指标包支持更细的 `rate` 窗口与多 series 聚合；
3. 把真实场景纳入 CI 门禁（当前需 ECS + 真实故障，成本高）。

---

## 5. 这次评测顺带修掉的产品缺陷

| 缺陷 | 现象 | 修复 |
|---|---|---|
| 指标覆盖缺口 | 磁盘 IO 只有 counter，目录没有绑定也没有 rate 语义，磁盘场景 0/3 | M10.1 扩展指标包 + gauge/counter，磁盘 Rank1 ✅ |
| 非因果结论抢 Rank1 | “日志 0 条、暂无证据支持…”置信度 0.9，把 CPU 候选挤到 Rank2 | `resolve_conflicts` 过滤“暂无证据/无样本/不足以/排除…”等非因果陈述 |
| 扩展信号越权 | CPU 压力也会抬高磁盘 IO，若无条件把扩展指标当根因会误判 CPU | 仅当扩展变化 ≥100% 且 ≥主指标变化 5 倍才生成确定性候选 |
| 故障进程泄漏 | CPU 的 `yes` 子进程在下一个场景仍在跑 | 新会话 + 进程组 kill + marker 预清理 |
| 关键词假阳性 | `exception` 里的 "io" 被算作磁盘命中 | ASCII 词边界匹配，重算结果 |
| 影响面 NaN | `histogram_quantile` 无样本返回 NaN，MCP JSON 序列化失败 | `_range_values` 过滤非有限值 |

---

## 6. 复现

```bash
# 在 ECS 上（会真实注入 CPU/内存/磁盘故障，约 4 分钟）
cd /opt/copilot
.venv/bin/python scripts/real_eval.py --scenarios cpu,memory,disk

# 只重算分数，不重跑分析
.venv/bin/python scripts/real_eval.py --rescore
```

环境变量：

```bash
FAULT_SECONDS=180        # 每个故障持续时间
WARMUP_SECONDS=45        # 注入后等待 Prometheus 采样
ANALYSIS_WINDOW=3m       # 分析窗口
```

---

## 7. 边界

- 只有 3 个场景、每场景 1 次，**不是统计显著性结论**。
- 故障是主机级资源注入，不是业务代码 bug；与 devstack 的 9 类应用故障互补。
- 评测跑在 ECS 上，依赖真实 ECS 时钟、Prometheus 采样与真实 LLM，不适合每次
  CI 都跑；CI 继续用 63 场景的 deterministic harness。
