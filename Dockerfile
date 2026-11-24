# Production image for the core API + MCP stdio servers.
#
# Build context is the repository root, not services/core, because the app
# spawns modules under services/mcp_servers and needs migrations/docs only at
# deploy time (not in the runtime image).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv:/srv/services/core

WORKDIR /srv

COPY services/core/requirements.txt /tmp/requirements.txt
# Aliyun PyPI mirror keeps the image build reproducible from mainland China.
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" \
    --trusted-host "$PIP_TRUSTED_HOST" -r /tmp/requirements.txt

COPY services /srv/services

RUN useradd -r -u 10001 appuser && chown -R appuser /srv
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["uvicorn", "app.api:app", "--app-dir", "/srv/services/core", \
     "--host", "0.0.0.0", "--port", "8000"]
