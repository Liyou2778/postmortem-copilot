# M10.7：Chaos Mesh 与负对照

> 环境：k3d 单节点 / k3s v1.30.4  
> Chaos Mesh：2.8.8（Helm）  
> 生产：K8s 2 副本，公网 :8000 -> socat -> NodePort

---

## 1. Chaos Mesh 安装

```bash
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  -n chaos-mesh --create-namespace --version 2.8.8 \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/k3s/containerd/containerd.sock \
  --set dashboard.create=true --wait
```

镜像在 Docker Hub/ghcr 直连受限时，通过 NJU ghcr mirror 拉取并预加载：

```text
chaos-mesh
chaos-daemon
chaos-dashboard
chaos-coredns
```

验收：

```text
chaos-controller-manager 3/3 Running
chaos-daemon 1/1 Running
chaos-dashboard 1/1 Running
chaos-dns-server 1/1 Running
CRD：PodChaos/NetworkChaos/IOChaos/StressChaos/... 已注册
```

---

## 2. PodChaos：pod-kill 实验

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: copilot-pod-kill
  namespace: copilot
spec:
  action: pod-kill
  mode: one
  duration: "30s"
  selector:
    namespaces: [copilot]
    labelSelectors:
      app.kubernetes.io/name: postmortem-copilot
      app.kubernetes.io/instance: copilot
```

执行与结果：

```text
chaos-mesh 选中并 kill 1 个 core pod
Deployment 立即创建替代 pod
公网 healthz 连续 8 次 200
新 pod 47s 内 Ready
Chaos 实验删除后无残留
```

这验证了 Pod 级自愈和 Service 多副本可用性；**不验证跨节点 HA**，因为 k3d 只有 1 个节点。

---

## 3. 负对照（无故障窗口）

脚本：

```bash
python scripts/negative_control.py
```

目标不是“永远零误报”，而是暴露回归：Mock 证据、工具失败、伪造业务数字、
高置信度非故障结论。

第一次运行发现两个真实缺陷：

| 缺陷 | 现象 | 修复 |
|---|---|---|
| 非因果结论漏网 | “变更窗口内未记录任何部署…无法归因于变更”置信度 0.9 进 Top | 增加“未记录任何/无法归因/无法说明”过滤 |
| 相对变化误报 | 磁盘 IO 0.001→0.004（0.4% busy）被报告为 +363% “饱和” | 确定性扩展候选增加绝对阈值：IO time peak ≥ 0.2 |

修复后负对照：

```text
claims=3
high_confidence_claims=[]
evidence_modes={real:8}
tool_failures=[]
failed_steps={}
business_impact.configured=false（不伪造金额）
```

---

## 4. K8s/Chaos 环境下真实评测复跑（回归记录）

```text
cpu:    Rank1 ✅ / Recall@3 ✅ / 8/8 real / impact ✅
memory: Rank1 ✅ / Recall@3 ✅ / 8/8 real / impact ✅
disk:   Rank1 ✅ / Recall@3 ✅ / 8/8 real / impact ✅
总体：Rank1 3/3，Recall@3 3/3
```

修复不是调关键词：CPU 场景改为读取 `node_cpu_seconds_total` 并转换成
`100 * (1 - avg(rate(...{mode="idle"})))` 的利用率，确定性候选直接在
利用率 ≥70% 时产生。中间 2/3/0/1 的回归记录保留在文档中，最终结果以
当前代码 + 最新一次完整评测为准。

---

## 5. NetworkChaos 尝试

```yaml
action: delay
direction: to
latency: 200ms / jitter 50ms
mode: one
```

第一次直接写 `direction: both` 被 admission webhook 拒绝（无 targets 时
from/both 不合法），改为 `to` 后创建成功；公网 healthz 仍为 200（请求可能
落到未注入的另一个 Pod，因此该实验没有证明端到端延迟）。实验删除时
finalizer 一度挂住，已用 merge patch 清理。

结论：NetworkChaos 只完成了“可创建/可清理”的链路验证，未完成可观测的
端到端网络故障验收。

---

## 6. StressChaos 与 CI 门禁

StressChaos CPU：

```yaml
action: cpu stress
workers: 2 / load: 100
mode: one / duration: 25s
```

结果：资源创建成功、公网 healthz 连续 200、实验删除后无残留；
但没有观测到可归因的 CPU 延迟/错误，因此只算“链路可创建/可清理”。

CI 门禁：

```bash
python -m unittest discover -s services/core/tests
python scripts/eval_gate.py --eval eval-real-m9.json
```

`eval_gate.py` 要求：

```text
3/3 Rank1
3/3 Recall@3
每个场景 evidence_real == evidence_total
impact_present == true
所有 tool_calls ok
```

当前运行结果：

```text
PASS: 3/3 rank1, 3/3 recall, 8/8 real per scenario, impact present, tools healthy
```

GitHub Actions 入口：`.github/workflows/eval-gate.yml`（单元测试 + 评测 artifact 门禁）。

---

## 7. 边界

- 只跑了 pod-kill；NetworkChaos/IOChaos/StressChaos 尚未纳入验收；
- 负对照仅 1 次窗口，不是统计意义上的误报率；
- k3d 单节点，不能证明跨节点故障转移；
- Chaos Mesh 组件本身占用节点资源，需在生产容量评估中计入；
- PRD 100% 的“Chaos 场景库/CI 门禁”仍未完成。
