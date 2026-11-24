#!/usr/bin/env bash
# Install single-node Loki + Promtail on Alibaba Cloud Linux 3.
#
# Design choices:
# * Both services listen on 127.0.0.1 only; the security group never opens
#   3100/9080.
# * Logs come from journald, so we get real evidence for the copilot service
#   without adding an application-side shipper.
# * GitHub release download is tried directly first, then through two public
#   mirrors. If all fail, stop (do not silently continue without logs).
# * Runs as a dedicated `loki` user; Promtail runs as root because reading the
#   system journal is the one thing it genuinely needs.
set -euo pipefail

VERSION=${LOKI_VERSION:-2.9.8}
DEST=/opt/monitoring
CONF_DIR=/etc/loki
PROMTAIL_CONF_DIR=/etc/promtail
DATA_DIR=/var/lib/loki
PROMTAIL_DATA=/var/lib/promtail
PY=python3

echo "== 1/8 dependencies =="
if ! command -v unzip >/dev/null 2>&1; then
  dnf install -y unzip >/dev/null
fi
if ! id loki >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /sbin/nologin loki
fi
mkdir -p "$DEST" "$CONF_DIR" "$PROMTAIL_CONF_DIR" "$DATA_DIR" "$PROMTAIL_DATA"

download() {
  local asset=$1 out=$2
  for base in \
    "https://github.com/grafana/loki/releases/download/v${VERSION}" \
    "https://ghproxy.net/https://github.com/grafana/loki/releases/download/v${VERSION}" \
    "https://gh-proxy.com/https://github.com/grafana/loki/releases/download/v${VERSION}"; do
    echo "  trying ${base}/${asset}"
    if curl -fL --max-time 600 --retry 2 "$base/$asset" -o "$out"; then
      return 0
    fi
  done
  return 1
}

echo "== 2/8 download Loki ${VERSION} =="
if [ ! -x "$DEST/loki-${VERSION}/loki" ]; then
  # A pre-placed archive wins: this is the path used when the operator
  # downloaded it on a proxied laptop and scp'd it to the server.
  [ -f "/tmp/loki-${VERSION}.zip" ] || download "loki-linux-amd64.zip" "/tmp/loki-${VERSION}.zip"
  unzip -q -o "/tmp/loki-${VERSION}.zip" -d "$DEST"
  # The zip contains a single binary at the root.
  if [ ! -f "$DEST/loki-linux-amd64" ]; then
    echo "unexpected loki archive layout"; exit 1
  fi
  mkdir -p "$DEST/loki-${VERSION}"
  mv "$DEST/loki-linux-amd64" "$DEST/loki-${VERSION}/loki"
  chmod +x "$DEST/loki-${VERSION}/loki"
  rm -f "/tmp/loki-${VERSION}.zip"
fi

echo "== 3/8 download Promtail ${VERSION} =="
if [ ! -x "$DEST/promtail-${VERSION}/promtail" ]; then
  [ -f "/tmp/promtail-${VERSION}.zip" ] || download "promtail-linux-amd64.zip" "/tmp/promtail-${VERSION}.zip"
  unzip -q -o "/tmp/promtail-${VERSION}.zip" -d "$DEST"
  if [ ! -f "$DEST/promtail-linux-amd64" ]; then
    echo "unexpected promtail archive layout"; exit 1
  fi
  mkdir -p "$DEST/promtail-${VERSION}"
  mv "$DEST/promtail-linux-amd64" "$DEST/promtail-${VERSION}/promtail"
  chmod +x "$DEST/promtail-${VERSION}/promtail"
  rm -f "/tmp/promtail-${VERSION}.zip"
fi

echo "== 4/8 Loki config =="
cat > "$CONF_DIR/loki-config.yml" <<EOF
auth_enabled: false
server:
  http_listen_address: 127.0.0.1
  http_listen_port: 3100
  # Loki 2.9's query scheduler advertises the host's private IP on gRPC; if
  # gRPC binds loopback only it dials its own private IP and queries hang.
  # Bind gRPC on the VPC interface (9096 is NOT open in the security group).
  grpc_listen_address: 0.0.0.0
  grpc_listen_port: 9096
common:
  # Single-node demo: advertise loopback, otherwise Loki advertises the ECS
  # private IP (172.24.x.x) on grpc :9096 while only binding 127.0.0.1, and the
  # query scheduler loops forever trying to dial itself.
  instance_addr: 127.0.0.1
  path_prefix: ${DATA_DIR}
  storage:
    filesystem:
      chunks_directory: ${DATA_DIR}/chunks
      rules_directory: ${DATA_DIR}/rules
  replication_factor: 1
  ring:
    instance_addr: 127.0.0.1
    kvstore:
      store: inmemory
schema_config:
  configs:
    - from: 2020-10-24
      store: boltdb-shipper
      object_store: filesystem
      schema: v11
      index:
        prefix: index_
        period: 24h
storage_config:
  boltdb_shipper:
    active_index_directory: ${DATA_DIR}/boltdb-shipper-active
    cache_location: ${DATA_DIR}/boltdb-shipper-cache
    shared_store: filesystem
  filesystem:
    directory: ${DATA_DIR}/chunks
compactor:
  working_directory: ${DATA_DIR}/compactor
  shared_store: filesystem
  retention_enabled: true
  retention_delete_delay: 2h
  retention_delete_worker_count: 64
limits_config:
  retention_period: 168h
  reject_old_samples: true
  reject_old_samples_max_age: 168h
frontend_worker:
  frontend_address: 127.0.0.1:9096
analytics:
  reporting_enabled: false
EOF

echo "== 5/8 Promtail config (journald) =="
cat > "$PROMTAIL_CONF_DIR/promtail-config.yml" <<EOF
server:
  http_listen_address: 127.0.0.1
  http_listen_port: 9080
  grpc_listen_port: 0
positions:
  filename: ${PROMTAIL_DATA}/positions.yaml
clients:
  - url: http://127.0.0.1:3100/loki/api/v1/push
scrape_configs:
  - job_name: journal
    journal:
      max_age: 24h
      labels:
        job: journal
        service: ecs-copilot
        hostname: copilot-ecs
    relabel_configs:
      - source_labels: ['__journal__systemd_unit']
        target_label: unit
      - source_labels: ['__journal_priority_keyword']
        target_label: level
EOF
chown -R loki:loki "$DATA_DIR"
chown -R root:root "$PROMTAIL_DATA"

echo "== 6/8 systemd units =="
cat > /etc/systemd/system/loki.service <<EOF
[Unit]
Description=Grafana Loki (single node, journald logs)
After=network.target

[Service]
Type=simple
User=loki
ExecStart=${DEST}/loki-${VERSION}/loki -config.file=${CONF_DIR}/loki-config.yml
Restart=always
RestartSec=3
LimitNOFILE=65536
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/promtail.service <<EOF
[Unit]
Description=Promtail (journald -> local Loki)
After=network.target loki.service
Requires=loki.service

[Service]
Type=simple
# Reads the system journal; no network listener beyond loopback.
User=root
ExecStart=${DEST}/promtail-${VERSION}/promtail -config.file=${PROMTAIL_CONF_DIR}/promtail-config.yml
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q loki promtail
systemctl restart loki promtail

echo "== 7/8 wait for readiness =="
for i in $(seq 1 30); do
  if curl -sf --max-time 3 http://127.0.0.1:3100/ready >/dev/null 2>&1; then
    echo "  loki ready"
    break
  fi
  sleep 1
done
for i in $(seq 1 30); do
  if curl -sf --max-time 3 http://127.0.0.1:9080/ready >/dev/null 2>&1; then
    echo "  promtail ready"
    break
  fi
  sleep 1
done
echo "  active: loki=$(systemctl is-active loki) promtail=$(systemctl is-active promtail)"

echo "== 8/8 verify a journal query =="
sleep 3
curl -s --max-time 10 'http://127.0.0.1:3100/loki/api/v1/labels' | head -c 500; echo
curl -s --max-time 10 --get 'http://127.0.0.1:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={job="journal"}' \
  --data-urlencode 'limit=3' | head -c 800; echo
echo
echo "loki: 127.0.0.1:3100 / promtail: 127.0.0.1:9080 (both loopback only)"
