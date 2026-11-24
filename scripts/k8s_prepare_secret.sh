#!/usr/bin/env bash
# Create/refresh the Kubernetes Secret from the production .env.
# Never commit the generated env file; it contains DB/OSS/LLM credentials.
set -eu
ENV_FILE=${ENV_FILE:-/opt/copilot/.env}
OUT=${OUT:-/root/copilot-k8s.env}
NAMESPACE=${NAMESPACE:-copilot}

python3 - "$ENV_FILE" "$OUT" <<'PY'
import sys
from pathlib import Path

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
lines = []
for raw in src.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    lines.append(f"{key}={value}")
dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
dst.chmod(0o600)
print(f"wrote {len(lines)} keys to {dst}")
PY

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic copilot-env -n "$NAMESPACE" \
  --from-env-file="$OUT" --dry-run=client -o yaml | kubectl apply -f -
kubectl get secret copilot-env -n "$NAMESPACE"
