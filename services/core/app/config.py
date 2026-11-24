"""Runtime settings.

Reads a `.env` file from the project root (falls back to process env), so
secrets never live in code and a teammate can start with one filled template.

Precedence: process environment > .env file > defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"
OIDC_ENV_FILE = PROJECT_ROOT / ".env.oidc"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE, # comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


_FILE_VALUES = _parse_env_file(ENV_FILE)
# OIDC secrets live in a separate 0600 file so they can be rotated without
# rewriting the main env. Main .env wins if a key exists in both.
for _key, _value in _parse_env_file(OIDC_ENV_FILE).items():
    _FILE_VALUES.setdefault(_key, _value)


def get(key: str, default: str = "") -> str:
    return os.environ.get(key) or _FILE_VALUES.get(key) or default


@dataclass(frozen=True)
class Settings:
    # --- model ---
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2
    # Optional cost accounting; 0 means "unit price not configured".
    llm_cost_per_1k_input: float = 0.0
    llm_cost_per_1k_output: float = 0.0
    llm_currency: str = "CNY"

    # --- storage / infra ---
    db_path: str = "core.db"
    redis_url: str = "redis://127.0.0.1:6379/0"

    # --- behaviour ---
    agent_max_retries: int = 2
    agent_token_budget: int = 40_000
    agent_tool_budget: int = 25
    step_deadline_ms: int = 180_000
    conflict_min_confidence: float = 0.35
    max_replans: int = 3

    # --- tools ---
    tool_timeout_s: float = 20.0
    tool_rate_limit_per_min: int = 60
    tool_url_allowlist: tuple[str, ...] = ("localhost", "127.0.0.1", "prometheus", "loki")
    # "local" uses in-process fixtures; "mcp" talks to the stdio MCP servers.
    tool_backend: str = "local"
    # "shared" = one process-wide gateway reused by every analysis;
    # "per_run" = old behaviour, one gateway per Orchestrator (debug/rollback).
    mcp_gateway_mode: str = "shared"
    # Ping/restart interval for the shared gateway; 0 disables the monitor.
    mcp_health_interval_s: float = 30.0
    # Upstream URLs the MCP servers fall back to when the service catalog has
    # no explicit datasource binding. Kept as settings (not module constants) so
    # a deploy can switch from devstack to a real Prometheus without code edits.
    prometheus_url: str = "http://127.0.0.1:9090"
    loki_url: str = "http://127.0.0.1:3100"
    cmdb_url: str = "http://127.0.0.1:9200"
    deploy_url: str = "http://127.0.0.1:9200"
    # Role tokens for the CMDB v2 control-plane API. They stay server-side;
    # the admin UI never exposes them to the browser.
    cmdb_token_viewer: str = ""
    cmdb_token_editor: str = ""
    cmdb_token_approver: str = ""
    cmdb_tenant: str = "team-core"
    cmdb_platform_token: str = ""
    kb_url: str = "http://127.0.0.1:9100"
    devstack_url: str = "http://127.0.0.1:9100"
    # Seed the catalog with the two known services on first start. Set to
    # false in tests / environments where the catalog is managed externally.
    catalog_seed: bool = True
    # Optional HTTP Basic auth for /admin/* and the catalog write API. Empty
    # password disables auth (only acceptable for a local demo instance).
    admin_user: str = "admin"
    admin_password: str = ""
    # M10.6 local identity/tenant layer. Disabled by default so the existing
    # public demo and unit tests keep their old behavior; production turns it on.
    auth_enabled: bool = False
    auth_session_hours: int = 12
    rate_limit_per_min: int = 120
    bootstrap_admin_password: str = ""
    # OTLP/HTTP export (e.g. http://otel-collector:4318). Empty disables.
    otel_exporter_endpoint: str = ""
    otel_service_name: str = "postmortem-copilot"
    # SSO via a trusted reverse proxy (oauth2-proxy / ALB / oauth2-proxy in
    # front of OIDC). Empty header disables the path. The proxy must strip
    # client-supplied copies and set the shared secret; we never trust a
    # header without that secret.
    auth_trusted_header: str = ""
    auth_trusted_secret: str = ""
    # Standard OIDC authorization-code flow.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_state_secret: str = ""
    # "sqlite" keeps everything in one file; "mysql" points at RDS.
    db_backend: str = "sqlite"
    db_host: str = ""
    db_port: int = 3306
    db_user: str = ""
    db_password: str = ""
    db_name: str = "copilot"

    # "sqlite" keeps everything in one file; "redis" is the production queue.
    queue_backend: str = "sqlite"

    # --- alert ingestion / notification ---
    # Shared secret expected in the X-Alert-Token header; empty means no auth
    # (suitable only for a demo instance that is not internet-exposed).
    alert_token: str = ""
    # Sliding window for per-service dedup, so an alert storm creates one task.
    alert_dedup_window_s: int = 1800
    # Comma-separated robot webhooks (DingTalk / Feishu / generic JSON).
    notify_webhook_urls: str = ""
    # Base URL used in notification links (falls back to the request base).
    public_base_url: str = ""

    # --- long-term memory (ContextDB v3 memory API, OAuth session auth) ---
    contextdb_oauth_base: str = "https://api.cn-hangzhou.agentcontext.aliyuncs.com"
    contextdb_oauth_access_token: str = ""
    contextdb_oauth_refresh_token: str = ""
    contextdb_user_id: str = "liyou"
    contextdb_agent_id: str = "postmortem-copilot"
    # knowledge-base path (Token auth) retained for non-memory use
    contextdb_endpoint: str = ""
    contextdb_api_key: str = ""
    memory_kb_name: str = "postmortem-history"

    # --- object storage (optional archival) ---
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_prefix: str = "postmortems/"

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    def masked(self) -> dict[str, object]:
        """Safe-to-print view; never leaks the key."""
        key = self.llm_api_key
        masked_key = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else ("<unset>" if not key else "***")
        return {
            "llm_base_url": self.llm_base_url or "<unset>",
            "llm_model": self.llm_model or "<unset>",
            "llm_api_key": masked_key,
            "llm_configured": self.llm_configured,
            "db_path": self.db_path,
            "redis_url": self.redis_url,
        }


def load_settings() -> Settings:
    return Settings(
        llm_base_url=get("LLM_BASE_URL").rstrip("/"),
        llm_api_key=get("LLM_API_KEY"),
        llm_model=get("LLM_MODEL"),
        llm_timeout_s=float(get("LLM_TIMEOUT_S", "60")),
        llm_max_retries=int(get("LLM_MAX_RETRIES", "2")),
        llm_cost_per_1k_input=float(get("LLM_COST_PER_1K_INPUT", "0")),
        llm_cost_per_1k_output=float(get("LLM_COST_PER_1K_OUTPUT", "0")),
        llm_currency=get("LLM_CURRENCY", "CNY").strip() or "CNY",
        db_path=get("CORE_DB_PATH", "core.db"),
        db_backend=get("DB_BACKEND", "sqlite").strip().lower(),
        db_host=get("DB_HOST", "").strip(),
        db_port=int(get("DB_PORT", "3306")),
        db_user=get("DB_USER", "").strip(),
        db_password=get("DB_PASSWORD", ""),
        db_name=get("DB_NAME", "copilot").strip(),
        redis_url=get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        agent_max_retries=int(get("AGENT_MAX_RETRIES", "2")),
        agent_token_budget=int(get("AGENT_TOKEN_BUDGET", "40000")),
        agent_tool_budget=int(get("AGENT_TOOL_BUDGET", "25")),
        step_deadline_ms=int(get("STEP_DEADLINE_MS", "180000")),
        conflict_min_confidence=float(get("CONFLICT_MIN_CONFIDENCE", "0.35")),
        max_replans=int(get("MAX_REPLANS", "3")),
        tool_timeout_s=float(get("TOOL_TIMEOUT_S", "20")),
        tool_rate_limit_per_min=int(get("TOOL_RATE_LIMIT_PER_MIN", "60")),
        tool_url_allowlist=tuple(
            host.strip().lower()
            for host in get(
                "TOOL_URL_ALLOWLIST", "localhost,127.0.0.1,prometheus,loki"
            ).split(",")
            if host.strip()
        ),
        tool_backend=get("TOOL_BACKEND", "local").strip().lower(),
        mcp_gateway_mode=get("MCP_GATEWAY_MODE", "shared").strip().lower(),
        mcp_health_interval_s=float(get("MCP_HEALTH_INTERVAL_S", "30")),
        prometheus_url=get("PROMETHEUS_URL", "http://127.0.0.1:9090").strip().rstrip("/"),
        loki_url=get("LOKI_URL", "http://127.0.0.1:3100").strip().rstrip("/"),
        cmdb_url=get("CMDB_URL", "http://127.0.0.1:9200").strip().rstrip("/"),
        deploy_url=get("DEPLOY_URL", "http://127.0.0.1:9200").strip().rstrip("/"),
        cmdb_token_viewer=get("CMDB_TOKEN_VIEWER", "").strip(),
        cmdb_token_editor=get("CMDB_TOKEN_EDITOR", "").strip(),
        cmdb_token_approver=get("CMDB_TOKEN_APPROVER", "").strip(),
        kb_url=get("KB_URL", get("DEVSTACK_URL", "http://127.0.0.1:9100")).strip().rstrip("/"),
        devstack_url=get("DEVSTACK_URL", "http://127.0.0.1:9100").strip().rstrip("/"),
        catalog_seed=get("CATALOG_SEED", "true").strip().lower() not in ("0", "false", "no"),
        admin_user=get("ADMIN_USER", "admin").strip() or "admin",
        admin_password=get("ADMIN_PASSWORD", ""),
        auth_enabled=get("AUTH_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"),
        auth_session_hours=int(get("AUTH_SESSION_HOURS", "12")),
        rate_limit_per_min=int(get("RATE_LIMIT_PER_MIN", "120")),
        bootstrap_admin_password=get("BOOTSTRAP_ADMIN_PASSWORD", ""),
        otel_exporter_endpoint=get("OTEL_EXPORTER_ENDPOINT", "").strip(),
        otel_service_name=get("OTEL_SERVICE_NAME", "postmortem-copilot").strip()
        or "postmortem-copilot",
        cmdb_tenant=get("CMDB_TENANT", "team-core").strip() or "team-core",
        cmdb_platform_token=get("CMDB_TOKEN_PLATFORM", "").strip(),
        oidc_issuer=get("OIDC_ISSUER", "").strip().rstrip("/"),
        oidc_client_id=get("OIDC_CLIENT_ID", "").strip(),
        oidc_client_secret=get("OIDC_CLIENT_SECRET", ""),
        oidc_redirect_uri=get("OIDC_REDIRECT_URI", "").strip(),
        oidc_scopes=get("OIDC_SCOPES", "openid profile email").strip(),
        oidc_state_secret=get(
            "OIDC_STATE_SECRET", get("BOOTSTRAP_ADMIN_PASSWORD", "")
        ),
        queue_backend=get("QUEUE_BACKEND", "sqlite").strip().lower(),
        alert_token=get("ALERT_TOKEN", "").strip(),
        alert_dedup_window_s=int(get("ALERT_DEDUP_WINDOW_S", "1800")),
        notify_webhook_urls=get("NOTIFY_WEBHOOK_URLS", "").strip(),
        public_base_url=get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        contextdb_oauth_base=get("CONTEXTDB_OAUTH_BASE",
                                 "https://api.cn-hangzhou.agentcontext.aliyuncs.com").strip(),
        contextdb_oauth_access_token=get("CONTEXTDB_OAUTH_ACCESS_TOKEN", "").strip(),
        contextdb_oauth_refresh_token=get("CONTEXTDB_OAUTH_REFRESH_TOKEN", "").strip(),
        contextdb_user_id=get("CONTEXTDB_USER_ID", "liyou").strip() or "liyou",
        contextdb_agent_id=get("CONTEXTDB_AGENT_ID", "postmortem-copilot").strip()
        or "postmortem-copilot",
        contextdb_endpoint=get("CONTEXTDB_ENDPOINT", "").strip(),
        contextdb_api_key=get("CONTEXTDB_API_KEY", "").strip(),
        memory_kb_name=get("MEMORY_KB_NAME", "postmortem-history").strip() or "postmortem-history",
        oss_endpoint=get("OSS_ENDPOINT", "").strip(),
        oss_bucket=get("OSS_BUCKET", "").strip(),
        oss_access_key_id=get("OSS_ACCESS_KEY_ID", "").strip(),
        oss_access_key_secret=get("OSS_ACCESS_KEY_SECRET", "").strip(),
        oss_prefix=get("OSS_PREFIX", "postmortems/").strip() or "postmortems/",
    )


def oss_config(settings: Settings | None = None):
    """Build the OSS config object without importing the storage layer here."""
    from .archive import OssConfig

    s = settings or load_settings()
    return OssConfig(
        endpoint=s.oss_endpoint,
        bucket=s.oss_bucket,
        access_key_id=s.oss_access_key_id,
        access_key_secret=s.oss_access_key_secret,
        prefix=s.oss_prefix,
    )
