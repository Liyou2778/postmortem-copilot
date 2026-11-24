#!/usr/bin/env bash
# Probe candidate RDS endpoints without needing credentials:
# resolution first (VPC DNS) then TCP 3306. Run on the ECS.
set -u

HOSTS=(
  "rm-cn-64q521nv40001z.mysql.rds.aliyuncs.com"
  "rm-cn-64q521nv40001z.rwlb.rds.aliyuncs.com"
  "rm-cn-64q521nv40001z.mysql.rds.aliyuncs.com."
)

echo "=== DNS 解析 ==="
for h in "${HOSTS[@]}"; do
  ip=$(getent hosts "$h" | awk '{print $1}' | head -1)
  if [ -n "$ip" ]; then
    echo "  OK   $h -> $ip"
  else
    echo "  --   $h -> 无法解析"
  fi
done

echo
echo "=== TCP 3306 探测 ==="
for h in "${HOSTS[@]}"; do
  ip=$(getent hosts "$h" | awk '{print $1}' | head -1)
  [ -z "$ip" ] && continue
  if timeout 5 bash -c "echo > /dev/tcp/$h/3306" 2>/dev/null; then
    echo "  OK   $h:3306 可连接"
  else
    echo "  --   $h:3306 不可达（可能实例还在初始化）"
  fi
done

echo
echo "=== 本机网络身份 ==="
echo "  私网IP : $(curl -s --connect-timeout 3 http://100.100.100.200/latest/meta-data/private-ipv4)"
echo "  VPC    : $(curl -s --connect-timeout 3 http://100.100.100.200/latest/meta-data/vpc-id)"
