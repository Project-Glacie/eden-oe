"""
Eden API Gateway — OpenRouter Cloud Provider

Routes to openrouter.ai. No hardcoded models.
"""
import logging
from typing import AsyncGenerator

import httpx

from eden_gateway.providers.base import BaseProvider, GatewayModel
from eden_gateway.auth import get_api_key

log = logging.getLogger(__name__)


class OpenRouterProvider(BaseProvider):
    name = "openrouter"
    display_name = "OpenRouter (Cloud)"

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.base_url = self.config.get("base_url", "https://openrouter.ai/api/v1")
        self._client = httpx.AsyncClient(timeout=180.0)

    async def list_models(self) -> list[GatewayModel]:
        """Query OpenRouter for available models."""
        try:
            resp = await self._client.get(
                f"{self.base_url}/models",
                headers=self.get_auth_headers(),
                timeout=15.0,
            )
            if resp.status_code == 200:
                return [
                    GatewayModel(
                        id=f"openrouter/{m['id']}",
                        provider=self.name,
                        context_length=m.get("context_length", 0),
                        cost_per_1k_input=float(m.get("pricing", {}).get("prompt", 0)),
                        cost_per_1k_output=float(m.get("pricing", {}).get("completion", 0)),
                        description=m.get("name", m["id"]),
                    )
                    for m in resp.json().get("data", [])
                ]
        except Exception as e:
            log.warning(f"Failed to query OpenRouter: {e}")
        return []

    def get_auth_headers(self) -> dict:
        api_key = get_api_key("openrouter") or self.config.get("api_key", "")
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://eden-os.projectglacie.com",
            "X-Title": "Eden OS",
        }

    async def chat_completion(
        self, model: str, messages: list[dict], stream: bool = False,
        temperature: float = 0.7, max_tokens: int | None = None, **kwargs,
    ) -> dict | AsyncGenerator:
        backend_model = model.replace("openrouter/", "")
        payload = {"model": backend_model, "messages": messages, "stream": stream, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        headers = self.get_auth_headers()

        if stream:
            return self._stream(payload, headers)
        resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _stream(self, payload: dict, headers: dict) -> AsyncGenerator:
        async with self._client.stream("POST", f"{self.base_url}/chat/completions", json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line:
                    yield line + "\n\n"

    async def health_check(self) -> bool:
        try:
            if not get_api_key("openrouter"):
                return False
            resp = await self._client.get(f"{self.base_url}/models", headers=self.get_auth_headers(), timeout=10.0)
            return resp.status_code == 200
        except Exception:
            return False
