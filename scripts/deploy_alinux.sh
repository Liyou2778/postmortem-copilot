#!/usr/bin/env bash
# Deploy Postmortem Copilot on Alibaba Cloud Linux 3 (Python 3.11).
# Idempotent: safe to re-run.
set -euo pipefail

APP_DIR=/opt/copilot
PY="$APP_DIR/.venv/bin/python"
PIP="$APP_DIR/.venv/bin/pip"
MIRROR="https://mirrors.aliyun.com/pypi/simple/"
HOSTMIRROR="mirrors.aliyun.com"

echo "== 1/7 install python deps =="
cd "$APP_DIR"
"$PIP" install -q -r services/core/requirements.txt -i "$MIRROR" --trusted-host "$HOSTMIRROR"

echo "== 2/7 verify imports =="
"$PY" -c 'import fastapi, pydantic, starlette, mcp; print("fastapi", fastapi.__version__, "| pydantic", pydantic.VERSION, "| starlette", starlette.__version__)'

echo "== 3/7 run unit tests (proves 3.11 compatibility) =="
"$PY" -m unittest discover -s services/core/tests 2>&1 | tail -3

echo "== 4/7 prepare dirs and env =="
mkdir -p "$APP_DIR/data"
if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<'ENV'
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-1558ad842e95436ca5e82c9ebd379ef9
LLM_MODEL=deepseek-flash
LLM_TIMEOUT_S=60
LLM_MAX_RETRIES=2
LLM_COST_PER_1K_INPUT=0
LLM_COST_PER_1K_OUTPUT=0
LLM_CURRENCY=CNY
CORE_DB_PATH=/opt/copilot/data/core.db
REDIS_URL=redis://127.0.0.1:6379/0
QUEUE_BACKEND=sqlite
TOOL_BACKEND=mcp
TOOL_TIMEOUT_S=20
TOOL_RATE_LIMIT_PER_MIN=60
MCP_GATEWAY_MODE=shared
MCP_HEALTH_INTERVAL_S=30
PROMETHEUS_URL=http://127.0.0.1:9090
LOKI_URL=http://127.0.0.1:3100
DEVSTACK_URL=http://127.0.0.1:9101
CMDB_URL=http://127.0.0.1:9200
DEPLOY_URL=http://127.0.0.1:9200
ALIYUN_REGION=cn-hangzhou
CMDB_PROVIDER=aliyun
CMDB_CREDENTIAL_MODE=instance_role
CMDB_RAM_ROLE_NAME=copilot-cmdb-reader
CMDB_DEFAULT_SERVICE=ecs-copilot
CMDB_DEFAULT_HOST_ID=i-bp1gh7s0tybo7k602nzr
TOOL_URL_ALLOWLIST=localhost,127.0.0.1,prometheus,loki
CATALOG_SEED=true
AUTH_ENABLED=true
AUTH_SESSION_HOURS=12
RATE_LIMIT_PER_MIN=120
AGENT_MAX_RETRIES=2
AGENT_TOKEN_BUDGET=40000
AGENT_TOOL_BUDGET=25
STEP_DEADLINE_MS=180000
CONFLICT_MIN_CONFIDENCE=0.35
MAX_REPLANS=3
ENV
  chmod 600 "$APP_DIR/.env"
  echo "   .env created (mode 600)"
else
  echo "   .env already exists, left untouched"
fi

echo "== 5/7 install systemd units =="
# Devstack is the mock upstream for the four non-metric sources. It listens on
# 9101 because node_exporter owns 9100 on this host.
cat > /etc/systemd/system/copilot-devstack.service <<'UNIT'
[Unit]
Description=Postmortem Copilot Devstack (mock upstreams)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/copilot
ExecStart=/opt/copilot/.venv/bin/python -m uvicorn services.devstack.app:app \
    --host 127.0.0.1 --port 9101 --app-dir /opt/copilot
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/copilot-cmdb.service <<'UNIT'
[Unit]
Description=Postmortem Copilot CMDB + change registry
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/copilot
ExecStart=/opt/copilot/.venv/bin/python -m uvicorn services.cmdb.app:app \
    --host 127.0.0.1 --port 9200 --app-dir /opt/copilot
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/copilot-cmdb-sync.service <<'UNIT'
[Unit]
Description=Postmortem Copilot CMDB Aliyun inventory sync
After=network.target copilot-cmdb.service
Requires=copilot-cmdb.service

[Service]
Type=oneshot
WorkingDirectory=/opt/copilot
ExecStart=/opt/copilot/.venv/bin/python /opt/copilot/scripts/sync_aliyun_cmdb.py --apply --changes --hours 2
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
UNIT

cat > /etc/systemd/system/copilot-cmdb-sync.timer <<'UNIT'
[Unit]
Description=Run Postmortem Copilot CMDB Aliyun sync every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Unit=copilot-cmdb-sync.service

[Install]
WantedBy=timers.target
UNIT

cat > /etc/systemd/system/copilot.service <<'UNIT'
[Unit]
Description=Postmortem Copilot API
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/copilot
EnvironmentFile=/opt/copilot/.env
ExecStart=/opt/copilot/.venv/bin/python -m uvicorn app.api:app \
    --host 0.0.0.0 --port 8000 --app-dir /opt/copilot/services/core
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
# hardening: the service only needs to write its own data dir
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable -q copilot.service

echo "== 6/7 start services =="
systemctl enable -q copilot-devstack.service copilot-cmdb.service copilot-cmdb-sync.timer
systemctl restart copilot-devstack.service
systemctl restart copilot-cmdb.service
systemctl restart copilot-cmdb-sync.timer
systemctl restart copilot.service
sleep 4
systemctl is-active copilot-devstack.service || true
systemctl is-active copilot-cmdb.service || true
systemctl is-active copilot.service || true

echo "== 7/7 health check =="
for i in $(seq 1 15); do
  if curl -sf http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo "   healthz OK: $(curl -s http://127.0.0.1:8000/healthz)"
    break
  fi
  sleep 2
done
echo "   devstack: $(curl -s -m 5 http://127.0.0.1:9101/healthz || echo FAILED)"
echo "   cmdb: $(curl -s -m 5 http://127.0.0.1:9200/healthz || echo FAILED)"
echo "   cmdb-sync timer: $(systemctl is-active copilot-cmdb-sync.timer || true)"
echo "   prometheus: $(curl -s -m 5 http://127.0.0.1:9090/-/healthy || echo FAILED)"

# Register this deployment as a real change in the CMDB. Best effort: a CMDB
# outage must not fail a deploy.
CHANGE_SUMMARY=${CMDB_CHANGE_SUMMARY:-"copilot deploy $(date +%F-%H%M)"}
curl -s -m 5 -X POST http://127.0.0.1:9200/api/v1/changes \
  -H 'content-type: application/json' \
  -d "{\"service\":\"ecs-copilot\",\"summary\":\"$CHANGE_SUMMARY\",\"operator\":\"deploy-script\"}" \
  >/dev/null 2>&1 || true

echo
echo "---- summary ----"
echo "service : $(systemctl is-active copilot.service)"
echo "memory  : $(systemctl show -p MemoryCurrent --value copilot.service | awk '{printf "%.0f MB", $1/1024/1024}')"
echo "port    : $(ss -tlnp 2>/dev/null | grep -c ':8000' || true) listener(s) on 8000"
echo "db      : $(ls -lh "$APP_DIR/data/core.db" 2>/dev/null | awk '{print $5}' || echo 'not created yet (created on first task)')"
