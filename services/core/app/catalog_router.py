"""Catalog-aware wrapper around the MCP tool gateway.

The gateway knows *how* to talk to an MCP server; it deliberately knows nothing
about which upstream serves a given service. That decision belongs to the
service catalog, and this router is the only place where the two meet.

For every tool call that carries a ``service`` argument the router:

1. resolves ``service + kind`` to a datasource binding,
2. refuses to call when there is no binding (no silent fallback to another
   tenant's Prometheus),
3. injects the datasource URL, selector and metric,
4. injects the binding's ``data_mode`` so the result can be labelled
   ``real`` or ``mock`` downstream.

The wrapper is intentionally thin: it does not parse Prometheus responses, it
does not decide whether a result is good evidence, and it does not retry. Those
belong to the MCP server and the agents respectively.
"""
from __future__ import annotations

from typing import Any

from .catalog import CatalogStore, KINDS
from .config import Settings, load_settings
from .tools import SSRFBlocked, ToolResult, assert_url_allowed

_SERVER_KIND = {
    "prometheus": "prometheus",
    "loki": "loki",
    "cmdb": "cmdb",
    "deploy": "deploy",
    "kb": "kb",
}


class CatalogRouter:
    """Sync facade with the same ``call`` contract as ``MCPGateway``."""

    def __init__(
        self,
        inner: Any,
        catalog: CatalogStore,
        settings: Settings | None = None,
    ) -> None:
        self.inner = inner
        self.catalog = catalog
        self.settings = settings or load_settings()

    # ------------------------------------------------------------------ passing
    @property
    def audit(self) -> list[Any]:
        return getattr(self.inner, "audit", [])

    def specs(self) -> list[dict[str, Any]]:
        if hasattr(self.inner, "tools"):
            return list(self.inner.tools())  # type: ignore[no-any-return]
        if hasattr(self.inner, "specs"):
            return list(self.inner.specs())  # type: ignore[no-any-return]
        return []

    def close(self) -> None:
        if hasattr(self.inner, "close"):
            self.inner.close()

    def __enter__(self) -> "CatalogRouter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --------------------------------------------------------------------- call
    def call(self, name: str, **kwargs: Any) -> ToolResult:
        server = name.split(".", 1)[0]
        kind = _SERVER_KIND.get(server)
        if kind not in KINDS or "service" not in kwargs:
            return self.inner.call(name, **kwargs)

        service = str(kwargs.get("service") or "")
        binding = self.catalog.resolve(service, kind)
        if binding is None:
            return ToolResult(
                False,
                name,
                error=(
                    f"服务目录未配置 {service!r} 的 {kind} 数据源"
                    "（到 /admin/catalog 绑定，或确认绑定已启用）"
                ),
            )

        try:
            assert_url_allowed(binding["base_url"], self.settings.tool_url_allowlist)
        except SSRFBlocked as exc:
            return ToolResult(False, name, error=f"数据源 URL 被 SSRF 白名单拒绝: {exc}")

        call_kwargs = dict(kwargs)
        call_kwargs["datasource_url"] = binding["base_url"]
        call_kwargs["data_mode"] = binding["mode"]
        extra_metric_count = 0

        try:
            if kind == "prometheus":
                call_kwargs["selector"] = (
                    str(call_kwargs.get("selector") or "") or binding.get("selector") or ""
                )
                if name.endswith(".impact"):
                    import json as _json

                    call_kwargs["impact_policy"] = _json.dumps(
                        binding.get("impact_policy") or {}, ensure_ascii=False
                    )
                # ``impact_overview`` has its own metric parameters; only the
                # time-series query needs the catalog's primary/secondary
                # metric injected.
                if name.endswith(".query_range"):
                    slot = str(call_kwargs.pop("slot", "primary") or "primary")
                    metric_type = "gauge"
                    metric = str(call_kwargs.get("metric") or "")
                    if not metric:
                        if slot == "secondary":
                            metric = binding.get("secondary_metric") or ""
                            metric_type = binding.get("secondary_metric_type") or "gauge"
                        elif slot.startswith("extra:"):
                            try:
                                index = int(slot.split(":", 1)[1])
                            except ValueError:
                                index = -1
                            extras = binding.get("extra_metrics") or []
                            if index < 0 or index >= len(extras):
                                return ToolResult(
                                    False,
                                    name,
                                    error=(
                                        f"服务目录未配置 {service!r} 的 extra:{index} 指标"
                                        f"（{binding['datasource_name']}）"
                                    ),
                                )
                            item = extras[index]
                            metric = str(item.get("metric") or "")
                            metric_type = str(item.get("type") or "gauge")
                        else:
                            metric = binding.get("metric") or ""
                            metric_type = binding.get("metric_type") or "gauge"
                    if not metric:
                        label = "次要指标" if slot == "secondary" else "主指标"
                        return ToolResult(
                            False,
                            name,
                            error=(
                                f"服务目录未配置 {service!r} 的{label}"
                                f"（{binding['datasource_name']}）"
                            ),
                        )
                    call_kwargs["metric"] = metric
                    call_kwargs["metric_type"] = metric_type
                    if slot == "primary":
                        extra_metric_count = len(binding.get("extra_metrics") or [])
            elif kind == "loki":
                call_kwargs["selector"] = (
                    str(call_kwargs.get("selector") or "") or binding.get("selector") or ""
                )
        except Exception as exc:  # noqa: BLE001 - a bad catalog row must not crash a run
            return ToolResult(False, name, error=f"服务目录解析失败: {type(exc).__name__}: {exc}")

        result = self.inner.call(name, **call_kwargs)
        if result.ok and isinstance(result.data, dict):
            # The MCP server echoes data_mode; setdefault keeps a stronger
            # "fixture" downgrade intact while guaranteeing a label exists.
            result.data.setdefault("data_mode", binding["mode"])
            result.data.setdefault("datasource", binding["datasource_name"])
            result.data.setdefault("extra_metric_count", extra_metric_count)
        return result
