#!/usr/bin/env bash
# Bind host-side dependencies to the VPC interface so K8s pods can reach them.
# Security group keeps 9090/3100/9200/9101 closed to the public internet.
set -eu
BACKUP=/root/k8s-bind-backup
mkdir -p "$BACKUP"
cp /etc/systemd/system/prometheus.service "$BACKUP/prometheus.service"
cp /etc/loki/loki-config.yml "$BACKUP/loki-config.yml"
cp /etc/systemd/system/copilot-cmdb.service "$BACKUP/copilot-cmdb.service"
cp /etc/systemd/system/copilot-devstack.service "$BACKUP/copilot-devstack.service"

sed -i 's/--web.listen-address=127.0.0.1:9090/--web.listen-address=0.0.0.0:9090/' \
  /etc/systemd/system/prometheus.service
sed -i 's/^  http_listen_address: 127.0.0.1/  http_listen_address: 0.0.0.0/' \
  /etc/loki/loki-config.yml
sed -i 's/--host 127.0.0.1 --port 9200/--host 0.0.0.0 --port 9200/' \
  /etc/systemd/system/copilot-cmdb.service
sed -i 's/--host 127.0.0.1 --port 9101/--host 0.0.0.0 --port 9101/' \
  /etc/systemd/system/copilot-devstack.service

systemctl daemon-reload
systemctl restart prometheus loki copilot-cmdb copilot-devstack
sleep 3
ss -lntp | grep -E ':9090|:3100|:9200|:9101'
echo "backup: $BACKUP"
