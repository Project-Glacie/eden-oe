"""
Eden API Gateway — DeepSeek Cloud Provider

Routes to api.deepseek.com. API key from env or chest.db.
Passes through responses unmodified — transparent proxy.
"""
import logging
from typing import AsyncGenerator

import httpx

from eden_gateway.providers.base import BaseProvider, GatewayModel
from eden_gateway.auth import get_api_key

log = logging.getLogger(__name__)


class DeepSeekProvider(BaseProvider):
    name = "deepseek"
    display_name = "DeepSeek (Cloud)"

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.base_url = self.config.get("base_url", "https://api.deepseek.com")
        self._client = httpx.AsyncClient(timeout=180.0)

    async def list_models(self) -> list[GatewayModel]:
        try:
            resp = await self._client.get(
                f"{self.base_url}/v1/models",
                headers=self.get_auth_headers(),
                timeout=10.0,
            )
            if resp.status_code == 200:
                return [
                    GatewayModel(id=f"deepseek/{m['id']}", provider=self.name, description=m.get("id", ""))
                    for m in resp.json().get("data", [])
                ]
        except Exception as e:
            log.warning(f"Failed to query DeepSeek: {e}")
        return []

    def get_auth_headers(self) -> dict:
        api_key = get_api_key("deepseek") or self.config.get("api_key", "")
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def chat_completion(
        self, model: str, messages: list[dict], stream: bool = False,
        temperature: float = 0.7, max_tokens: int | None = None, **kwargs,
    ) -> dict | AsyncGenerator:
        backend_model = model.replace("deepseek/", "")
        # DeepSeek thinking models need sufficient tokens for reasoning + content
        if max_tokens is None or max_tokens < 200:
            max_tokens = 200
        payload = {"model": backend_model, "messages": messages, "stream": stream, "temperature": temperature, "max_tokens": max_tokens}
        headers = self.get_auth_headers()

        if stream:
            return self._stream(payload, headers)
        resp = await self._client.post(f"{self.base_url}/v1/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _stream(self, payload: dict, headers: dict) -> AsyncGenerator:
        """Passthrough SSE stream with proper double-newline event separation."""
        async with self._client.stream("POST", f"{self.base_url}/v1/chat/completions", json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line:
                    yield line + "\n\n"

    async def health_check(self) -> bool:
        try:
            if not get_api_key("deepseek"):
                return False
            resp = await self._client.get(f"{self.base_url}/v1/chat/completions/models", headers=self.get_auth_headers(), timeout=10.0)
            return resp.status_code == 200
        except Exception:
            try:
                resp = await self._client.get(f"{self.base_url}/models", headers=self.get_auth_headers(), timeout=10.0)
                return resp.status_code == 200
            except Exception:
                return False
