"""Provider registry."""
from __future__ import annotations

from app.data_providers.tickflow_provider import TickFlowProvider

_PROVIDERS = {
    "tickflow": TickFlowProvider,
}


def get_provider(name: str = "rustdx"):
    name = (name or "rustdx").lower()
    if name == "rustdx":
        from app.plugins.rustdx.provider import RustdxProvider

        return RustdxProvider()
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        from app.data_providers import custom as custom_sources

        return custom_sources.get_provider(name)
    return provider_cls()
