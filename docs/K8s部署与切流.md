# M10.5：K8s / Helm 部署与生产切流

> 上线：2026-11-23 20:28 CST
> 环境：ECS 单节点 k3d（k3s v1.30.4）  
> 公网：http://121.41.224.245:8000  
> 回滚：`bash /opt/copilot/scripts/k8s_rollback.sh`

---

## 1. 拓扑

```text
公网 :8000
  └── socat (systemd: copilot-k8s-proxy)
        └── k3d NodePort :18000
              └── Service postmortem-copilot:80
                    ├── pod core #1
                    └── pod core #2
```

宿主机继续承载有状态/基础服务：

```text
Prometheus :9090      (绑定 0.0.0.0，安全组未开放)
Loki       :3100
CMDB       :9200
Devstack   :9101
MySQL/OSS 外部托管
```

K8s 内的 core 通过 ECS 私网 IP `172.24.19.160` 访问这些服务；目录
datasource 的 base_url 已从 `127.0.0.1` 切到私网 IP。

---

## 2. 为什么 Pod 需要私网地址

Pod 的 `127.0.0.1` 是 Pod 自己，不是宿主机。因此：

1. 宿主机 Prometheus/Loki/CMDB/devstack 改为监听 `0.0.0.0`；
2. 安全组仍然只开放 22/8000，9090/3100/9200/9101 公网不可达（已实测）；
3. 目录 datasource 用 `172.24.19.160`；
4. `TOOL_URL_ALLOWLIST` 加入该 IP。

切换脚本可逆：

```bash
python scripts/switch_catalog_host.py --host 172.24.19.160 --apply
python scripts/switch_catalog_host.py --host 127.0.0.1 --apply
```

---

## 3. 镜像与 Chart

镜像：

```text
postmortem-copilot:m10.7x (M10 核心冻结；认证/租户 + DAG + OTel 导出适配器；mock receiver 已停止)
```

构建时使用阿里云 PyPI 镜像，避免中国大陆访问 PyPI 超时：

```bash
docker build -t postmortem-copilot:m10.7v /opt/copilot
k3d image import postmortem-copilot:m10.7v -c copilot
```

Chart：

```text
deploy/helm/postmortem-copilot/
  values.yaml        replicaCount=2 / NodePort 30080 / 资源限制
  deployment.yaml    RollingUpdate maxUnavailable=0, maxSurge=1, PDB
```

部署：

```bash
bash scripts/k8s_prepare_secret.sh
helm upgrade --install copilot deploy/helm/postmortem-copilot \
  --namespace copilot --wait --timeout 5m
```

Secret 从 `/opt/copilot/.env` 生成，不提交仓库。

---

## 4. 验收结果

```text
Pod：2/2 Running（同一节点，多副本）
Helm：STATUS=deployed
滚动更新：30 次探测全部 200，无中断
公网 healthz：200
匿名 API：401；/login：200
admin/cmdb 匿名：303；Basic Auth：200
端到端分析（API Key）：7 条证据全部 real，7 个工具调用 0 失败
trace：6 spans
成本：单价未配置（如实显示）
```

认证上线后，脚本调用需带：

```bash
export COPILOT_API_KEY=$(grep '^COPILOT_API_KEY=' /opt/copilot/.env | cut -d= -f2- | tr -d "'")
python scripts/run_analysis.py --base http://127.0.0.1:8000 ...
```

---

## 5. 切流与回滚

切流：

```bash
bash /opt/copilot/scripts/k8s_cutover.sh
```

动作：
1. 确认 k3d :18000 健康；
2. `systemctl disable --now copilot`（避免重启后与 :8000 冲突）；
3. 安装并启动 `copilot-k8s-proxy.service`（socat :8000 -> :18000）；
4. 验证公网 healthz。

回滚：

```bash
bash /opt/copilot/scripts/k8s_rollback.sh
```

动作：
1. 停用删除 socat proxy；
2. 目录 datasource 切回 `127.0.0.1`；
3. `systemctl enable --now copilot`；
4. 验证 healthz。

---

## 6. 已知边界（必须如实说明）

| 边界 | 说明 |
|---|---|
| 单节点 | k3d 只有 1 个 k3s server；Pod 2 副本在同一节点，不是真正跨节点 HA |
| 宿主机依赖 | Prometheus/Loki/CMDB/Devstack 仍在 systemd；K8s 只接管 core API |
| 无 Ingress/TLS | 公网仍是 `http://IP:8000`，socat 直通 |
| 镜像源受限 | Docker Hub/GitHub 部分不可达；pause/CoreDNS/工具镜像需预加载 |
| kubectl 宿主二进制异常 | 宿主 kubectl 偶发 segfault；使用 `docker exec k3d-copilot-server-0 kubectl ...` 或 Helm |
| 自愈范围 | Pod/进程级自愈；节点/ECS 故障不自动迁移 |

这些不是“已完成 PRD 100%”的证明；M10.5 只证明容器化、Helm、多副本、滚动更新和蓝绿切流可运行。
