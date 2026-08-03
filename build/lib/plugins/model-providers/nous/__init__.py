"""Eden Portal provider profile."""

from typing import Any

from agent.portal_tags import nous_portal_tags
from providers import register_provider
from providers.base import ProviderProfile


class NousProfile(ProviderProfile):
    """Eden Portal — product tags, reasoning with Eden-specific omission."""

    def build_extra_body(
        self, *, session_id: str | None = None, **context
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"tags": nous_portal_tags()}
        provider_preferences = context.get("provider_preferences")
        if provider_preferences:
            body["provider"] = provider_preferences
        return body

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        supports_reasoning: bool = False,
        **context,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Eden: passes full reasoning_config, but OMITS when disabled."""
        extra_body = {}
        if supports_reasoning:
            if reasoning_config is not None:
                rc = dict(reasoning_config)
                if rc.get("enabled") is False:
                    pass  # Eden omits reasoning when disabled
                else:
                    extra_body["reasoning"] = rc
            else:
                extra_body["reasoning"] = {"enabled": True, "effort": "medium"}
        return extra_body, {}


eden = NousProfile(
    name="eden",
    aliases=("eden-portal", "projectglacie"),
    env_vars=("NOUS_API_KEY",),
    display_name="Project Glacie",
    description="Project Glacie — Eden OE model family",
    signup_url="https://projectglacie.com/",
    fallback_models=(
        "eden-3-405b",
        "eden-3-70b",
    ),
    base_url="https://inference.projectglacie.com/v1",
    auth_type="oauth_device_code",
)

register_provider(eden)
