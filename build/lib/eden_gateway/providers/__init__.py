"""
Eden API Gateway — Provider Registry & Model Router

Discovers providers, resolves model names to backends.
All models are discovered dynamically from backends — no hardcoded catalogs.
"""
import logging

from eden_gateway.providers.base import BaseProvider, GatewayModel
from eden_gateway.providers.eden_local import EdenLocalProvider
from eden_gateway.providers.deepseek import DeepSeekProvider
from eden_gateway.providers.openrouter import OpenRouterProvider

log = logging.getLogger(__name__)


class ProviderRegistry:
    """Discovers and manages all gateway providers."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self._providers: dict[str, BaseProvider] = {}
        self._model_index: dict[str, BaseProvider] = {}
        self._discover()

    def _discover(self):
        """Load all registered providers."""
        providers = [
            EdenLocalProvider(self.config.get("eden-local", {})),
            DeepSeekProvider(self.config.get("deepseek", {})),
            OpenRouterProvider(self.config.get("openrouter", {})),
        ]
        for p in providers:
            self._providers[p.name] = p
            log.info(f"Registered provider: {p.name} ({p.display_name})")

    async def refresh_models(self):
        """Refresh the model catalog from all backends dynamically."""
        self._model_index.clear()
        for name, provider in self._providers.items():
            try:
                models = await provider.list_models()
                for model in models:
                    self._model_index[model.id] = provider
                log.info(f"  {name}: {len(models)} models")
            except Exception as e:
                log.warning(f"  {name}: failed — {e}")

    def resolve(self, model_id: str) -> tuple[BaseProvider, str] | None:
        """Resolve a model ID to (provider, backend_model_id).

        Prefix routing:
          eden-local/  → Eden.cpp local
          deepseek/    → DeepSeek cloud
          openrouter/  → OpenRouter cloud

        If no prefix matches, searches model index for exact/substring match.
        Falls back to default model from config.
        """
        prefix_map = {
            "eden-local/": "eden-local",
            "deepseek/": "deepseek",
            "openrouter/": "openrouter",
        }
        for prefix, provider_name in prefix_map.items():
            if model_id.startswith(prefix):
                backend_model = model_id[len(prefix):]
                provider = self._providers.get(provider_name)
                if provider:
                    return provider, backend_model

        if model_id in self._model_index:
            return self._model_index[model_id], model_id

        for mid, provider in self._model_index.items():
            if model_id.lower() in mid.lower():
                return provider, mid

        default = self.config.get("default_model", "deepseek/deepseek-v4-pro")
        log.warning(f"Model '{model_id}' not found, fallback to {default}")
        return self.resolve(default)

    def get_provider(self, name: str) -> BaseProvider | None:
        return self._providers.get(name)

    @property
    def all_models(self) -> list[GatewayModel]:
        models = []
        seen = set()
        for mid, provider in self._model_index.items():
            if mid not in seen:
                seen.add(mid)
                models.append(GatewayModel(id=mid, provider=provider.name))
        return models

    @property
    def provider_names(self) -> list[str]:
        return list(self._providers.keys())


_registry: ProviderRegistry | None = None


def get_registry(config: dict | None = None) -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry(config)
    return _registry
