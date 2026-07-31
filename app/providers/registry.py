"""
Maps provider name (as used in config.yaml) -> instantiated Provider.
Built once at startup from the loaded config.
"""
from __future__ import annotations

from app.config import GatewayConfig
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import Provider
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider

_PROVIDER_CLASSES = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
}


class ProviderRegistry:
    def __init__(self, config: GatewayConfig):
        self._providers: dict[str, Provider] = {}
        for name, provider_config in config.providers.items():
            provider_cls = _PROVIDER_CLASSES.get(name)
            if provider_cls is None:
                continue
            self._providers[name] = provider_cls(provider_config)

    def get(self, name: str) -> Provider | None:
        return self._providers.get(name)


_registry: ProviderRegistry | None = None


def build_registry(config: GatewayConfig) -> ProviderRegistry:
    global _registry
    _registry = ProviderRegistry(config)
    return _registry


def get_registry() -> ProviderRegistry:
    if _registry is None:
        raise RuntimeError("Provider registry not initialized yet")
    return _registry
