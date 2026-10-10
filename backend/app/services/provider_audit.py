"""Read-only audit of effective market-data provider routes."""
from __future__ import annotations

from typing import Any

from app.data_providers.capabilities import CAPABILITY_REGISTRY, build_capability_matrix
from app.services import preferences

_PROVIDER_GETTERS = {
    "daily_data_provider": preferences.get_daily_data_provider,
    "adj_factor_provider": preferences.get_adj_factor_provider,
    "realtime_data_provider": preferences.get_realtime_data_provider,
    "minute_data_provider": preferences.get_minute_data_provider,
    "depth5_data_provider": preferences.get_depth5_data_provider,
    "financial_data_provider": preferences.get_financial_provider,
    "full_minute_data_provider": preferences.get_full_minute_data_provider,
}


def current_provider_routes() -> dict[str, str]:
    """Return every routable dataset's effective provider preference."""
    return {
        field: getter()
        for field, getter in _PROVIDER_GETTERS.items()
    }


def audit_provider_routes(*, tickflow_tier: str | None = None) -> dict[str, Any]:
    """Audit route availability and make rustdx single-source drift visible.

    Independent per-dataset routing remains supported. Mixing rustdx with another
    provider is therefore a warning, not an automatic rewrite or fallback.
    """
    if tickflow_tier is None:
        from app.tickflow.policy import base_tier_name

        tickflow_tier = base_tier_name()

    current = current_provider_routes()
    matrix = build_capability_matrix(current, tickflow_tier=tickflow_tier)
    rows = matrix["capabilities"]
    routes = {
        str(row["id"]): {
            "provider": str(row["effective"]),
            "usable": bool(row["usable"]),
        }
        for row in rows
    }
    sources = sorted({route["provider"] for route in routes.values()})
    rustdx_routes = sorted(
        capability
        for capability, route in routes.items()
        if route["provider"] == "rustdx"
    )
    unavailable = sorted(
        capability
        for capability, route in routes.items()
        if not route["usable"]
    )
    all_capabilities = {str(capability["id"]) for capability in CAPABILITY_REGISTRY}
    rustdx_only = set(rustdx_routes) == all_capabilities

    issues: list[dict[str, str]] = []
    if rustdx_routes and not rustdx_only:
        issues.append({
            "code": "rustdx_mixed_sources",
            "severity": "warning",
            "message": "rustdx 已启用, 但并非所有数据能力都路由到 rustdx",
        })
    if unavailable:
        issues.append({
            "code": "provider_route_unavailable",
            "severity": "error" if "daily" in unavailable else "warning",
            "message": "当前数据源不可用的能力: " + ", ".join(unavailable),
        })

    status = "ok"
    if any(issue["severity"] == "error" for issue in issues):
        status = "error"
    elif issues:
        status = "warning"

    if rustdx_only:
        policy = "rustdx_only"
    elif rustdx_routes:
        policy = "mixed_with_rustdx"
    elif len(sources) == 1:
        policy = "single_source"
    else:
        policy = "independent_routes"

    return {
        "status": status,
        "healthy": status != "error",
        "policy": policy,
        "single_source": len(sources) == 1,
        "rustdx_only": rustdx_only,
        "mixed_sources": len(sources) > 1,
        "sources": sources,
        "routes": routes,
        "unavailable": unavailable,
        "issues": issues,
    }
