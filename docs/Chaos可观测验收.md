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

后续进一步排查并修复了根因：

```text
host 内核 ip_tables / iptable_filter 模块未加载
-> chaos-daemon 在 Pod netns 初始化 iptables 失败
-> NetworkChaos 实际未生效
```

修复：

```bash
modprobe ip_tables
modprobe iptable_filter
# 持久化
/etc/modules-load.d/chaos-iptables.conf
```

修复后，用普通 client Pod 直连 app Pod IP 测量：

```text
基线：
  10.42.0.116:8000 -> 37ms
  10.42.0.115:8000 -> 5ms

NetworkChaos delay 300ms + jitter 50ms，direction=both，target=client：
  10.42.0.116:8000 -> 1.255s
  10.42.0.115:8000 -> 1.229s
```

结论：**NetworkChaos delay 已产生可归因延迟，通过。**

注意：公网 NodePort 测量未观察到延迟，因为该客户端路径没有被 target selector 命中；
验收必须使用固定 client Pod 直连 app Pod IP 的测量口径。

### HTTPChaos delay：未通过

修复 iptables 模块后重试：

```text
target=Request，path=/healthz：200，无 300ms 延迟
target=Response，path=/healthz：200，无 300ms 延迟
target=Response，去掉 path：200，无延迟
```

不再出现 000 连接错误，但 HTTPChaos 没有产生可归因延迟。
再用固定 client 直连 app Pod IP 测试：

```text
baseline 10.42.0.115 -> 21.8ms
during  10.42.0.115 -> 44.5ms
```

仍无 300ms 延迟，在 k3d 下不能作为验收项。

### StressChaos CPU：通过

```text
CPU workers=2 load=100
节点 CPU 利用率（Prometheus）：
  baseline 18.2%
  during   53.17%
  after    28.13%
```

资源创建/清理正常，节点 CPU 有明显可归因上升。

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

## 3. 已确认可复现

```text
PodChaos pod-kill：通过
NetworkChaos delay：通过
  baseline 5–37ms -> during 1.23–1.25s，target=client，direction=both
修复：host 加载 ip_tables / iptable_filter，并写入 modules-load.d
```

### IOChaos latency：通过

```text
目标 Pod：/data/io-test 写入 + fsync
基线写入耗时：0.0026–0.0068s
IOChaos latency 100ms 注入后：0.9168–0.9382s
```

资源创建/清理正常，I/O 延迟可归因。

## 4. 仍需完成

```text
HTTPChaos：target=Request/Response 都出现 000 连接错误，未通过
```

## 5. 当前结论

```text
PodChaos：通过
NetworkChaos delay：通过
StressChaos CPU：通过
IOChaos latency：通过
HTTPChaos：未通过
Chaos 场景库仍缺 HTTPChaos，PRD 该项只能标部分完成
```
