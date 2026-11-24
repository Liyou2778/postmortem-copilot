#!/usr/bin/env bash
# Re-write the local Prometheus scrape config with the copilot app-metrics job.
# Idempotent; keeps a timestamped backup. Run on the ECS.
set -euo pipefail

CONF=/etc/prometheus/prometheus.yml
BACKUP=/etc/prometheus/prometheus.yml.bak.$(date +%F-%H%M)
cp "$CONF" "$BACKUP" 2>/dev/null || true

cat > "$CONF" <<'YAML'
global:
  scrape_interval: 15s
  evaluation_interval: 15s
scrape_configs:
  - job_name: node
    static_configs:
      - targets: ["127.0.0.1:9100"]
        labels:
          service: ecs-copilot
          instance_name: copilot-ecs
  - job_name: prometheus
    static_configs:
      - targets: ["127.0.0.1:9090"]
  - job_name: copilot
    static_configs:
      - targets: ["127.0.0.1:8000"]
        labels:
          service: ecs-copilot
          instance_name: copilot-api
YAML

systemctl restart prometheus
for i in $(seq 1 20); do
  if curl -sf --max-time 3 http://127.0.0.1:9090/-/ready >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
echo "config: $CONF (backup: $BACKUP)"
curl -s --max-time 5 http://127.0.0.1:9090/api/v1/targets \
  | tr ',' '\n' | grep -E '"job"|"health"' | head -12
