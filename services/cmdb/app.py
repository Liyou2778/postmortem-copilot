"""CMDB HTTP service entrypoint.

Run:
    uvicorn services.cmdb.app:app --host 127.0.0.1 --port 9200 --app-dir /opt/copilot

v1 (`/api/v1`) is the stable MCP compatibility facade; v2 (`/api/v2`) is the
enterprise CI graph / change workflow API protected by role tokens.
"""
from __future__ import annotations

from .graph import GraphStore
from .store import CmdbStore, create_app
from app.config import get, load_settings  # noqa: E402
from app.db import from_settings  # noqa: E402

_settings = load_settings()
_legacy = CmdbStore(from_settings(_settings))
_graph = GraphStore(_legacy.db)
_graph.seed_from_legacy(_legacy)

_tokens = {
    "viewer": get("CMDB_TOKEN_VIEWER").strip(),
    "editor": get("CMDB_TOKEN_EDITOR").strip(),
    "approver": get("CMDB_TOKEN_APPROVER").strip(),
}
_tokens = {role: token for role, token in _tokens.items() if token}

app = create_app(
    _legacy, _graph, _tokens,
    tenant=get("CMDB_TENANT", "team-core"),
    platform_token=get("CMDB_TOKEN_PLATFORM").strip(),
)
