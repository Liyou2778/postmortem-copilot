# Chaos 可观测验收（当前未通过）

> 目标：故障注入必须产生**可归因、可复现**的指标变化，不能只看“资源创建成功”。

## 1. 已有证据

### PodChaos pod-kill：通过

```text
模式：one
1 个 core Pod 被 kill
替代 Pod 47s Ready
公网 healthz 连续 8 次 200
实验删除后无残留
```

### NetworkChaos delay：未完成

尝试过：

```text
mode: all
delay: 300ms / jitter 50ms
direction: to
target: chaos-client pod
```

问题：

- 独立 client Pod 访问 Service 基线超时，测量链路未建立；
- 公网 healthz 未观察到稳定 300ms 延迟。

结论：只证明“可创建/可清理”，**没有可归因延迟证据**。

### HTTPChaos delay：未通过

```text
path: /healthz
target: Response
delay: 300ms
mode: all
```

结果：

```text
大部分 200 请求 6–47ms
1 次 000 / 2.2s
```

没有稳定 300ms 延迟，不能算通过。

### StressChaos：未通过

```text
CPU workers=2 load=100
```

资源可创建/清理，公网仍 200；没有可归因 CPU/延迟变化。

## 2. 建议验收 KPI

需要你确认或修改：

```text
NetworkChaos delay 300ms：
  - 从集群内固定 client 测量 P50/P95
  - P50 增加 >= 250ms
  - 请求成功率 >= 95%
  - 删除实验后 60s 内恢复

HTTPChaos delay：
  - 同样以 client 观测为准，不以偶发 000 作为通过

StressChaos CPU：
  - 目标 Pod CPU 利用率 >= 80%
  - 或公网 P95 增加 >= 200ms

IOChaos：
  - 需要明确挂载路径与目标文件，否则不做
```

## 3. 需要用户确认

1. 是否接受上述 KPI？
2. NetworkChaos 的 client 测量是否可以由我在集群内创建测试 Pod？
3. 是否必须覆盖 IOChaos，还是只做 Network/CPU/HTTP？

## 4. 当前结论

```text
PodChaos：通过
Network/HTTP/Stress/IO：未通过可观测验收
PRD 100% 的 Chaos 项不能标记完成
```
