#!/usr/bin/env bash
set -u
NJU=https://mirror.nju.edu.cn/github-release/prometheus
DEST=/opt/monitoring
mkdir -p "$DEST"

try_dl() {  # $1 repo  $2 tag  $3 file
  u="$NJU/$1/$2/$3"
  code=$(curl -s -o /dev/null -w '%{http_code}' -I --max-time 20 "$u")
  echo "  $code  $3"
  if [ "$code" = "200" ]; then
    curl -fsSL --max-time 300 "$u" -o "$DEST/$3" && tar -xzf "$DEST/$3" -C "$DEST" && return 0
  fi
  return 1
}

echo "== node_exporter =="
NE=""
for v in 1.8.2 1.9.0 1.7.0 1.6.1; do
  if try_dl node_exporter "v$v" "node_exporter-$v.linux-amd64.tar.gz"; then NE="$DEST/node_exporter-$v.linux-amd64"; break; fi
done

echo "== prometheus =="
PR=""
for v in 2.53.0 2.52.0 2.51.2 2.50.1; do
  if try_dl prometheus "v$v" "prometheus-$v.linux-amd64.tar.gz"; then PR="$DEST/prometheus-$v.linux-amd64"; break; fi
done

echo "NE=${NE:-NONE}"
echo "PR=${PR:-NONE}"
[ -z "$NE" ] || [ -z "$PR" ] && { echo "DOWNLOAD_FAILED"; exit 1; }

mkdir -p /etc/prometheus /var/lib/prometheus
printf '%s\n' \
'global:' \
'  scrape_interval: 15s' \
'  evaluation_interval: 15s' \
'scrape_configs:' \
'  - job_name: node' \
'    static_configs:' \
'      - targets: ["127.0.0.1:9100"]' \
'        labels:' \
'          service: ecs-copilot' \
'          instance_name: copilot-ecs' \
'  - job_name: prometheus' \
'    static_configs:' \
'      - targets: ["127.0.0.1:9090"]' \
'  - job_name: copilot' \
'    static_configs:' \
'      - targets: ["127.0.0.1:8000"]' \
'        labels:' \
'          service: ecs-copilot' \
'          instance_name: copilot-api' \
> /etc/prometheus/prometheus.yml

printf '[Unit]\nDescription=Node Exporter\nAfter=network.target\n[Service]\nExecStart=%s/node_exporter --web.listen-address=127.0.0.1:9100\nRestart=always\n[Install]\nWantedBy=multi-user.target\n' "$NE" > /etc/systemd/system/node-exporter.service
printf '[Unit]\nDescription=Prometheus\nAfter=network.target\n[Service]\nExecStart=%s/prometheus --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/var/lib/prometheus --web.listen-address=127.0.0.1:9090\nRestart=always\n[Install]\nWantedBy=multi-user.target\n' "$PR" > /etc/systemd/system/prometheus.service

systemctl daemon-reload
systemctl enable -q node-exporter prometheus
systemctl restart node-exporter prometheus
sleep 8
echo "ACTIVE=$(systemctl is-active node-exporter) $(systemctl is-active prometheus)"
echo "TARGETS:"
curl -s --max-time 8 http://127.0.0.1:9090/api/v1/targets | grep -oE '"health":"[a-z]+"' | sort | uniq -c
