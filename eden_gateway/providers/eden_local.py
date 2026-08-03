"""
Eden API Gateway — Eden.cpp Local Provider

Routes to local llama.cpp server (eden.cpp) at :9093.
No hardcoded models — queries backend dynamically.
"""
import logging
from typing import AsyncGenerator

import httpx

from eden_gateway.providers.base import BaseProvider, GatewayModel

log = logging.getLogger(__name__)


class EdenLocalProvider(BaseProvider):
    name = "eden-local"
    display_name = "Eden.cpp (Local)"

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.base_url = self.config.get("base_url", "http://127.0.0.1:9093")
        self._client = httpx.AsyncClient(timeout=120.0)

    async def list_models(self) -> list[GatewayModel]:
        """Query eden.cpp for available models."""
        try:
            resp = await self._client.get(f"{self.base_url}/v1/models", timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                return [
                    GatewayModel(
                        id=f"eden-local/{m['id'].replace('.gguf', '')}",
                        provider=self.name,
                        context_length=m.get("meta", {}).get("n_ctx_train", 0),
                        description=m.get("id", ""),
                    )
                    for m in data.get("data", [])
                ]
        except Exception as e:
            log.warning(f"Failed to query eden.cpp: {e}")
        return []

    async def chat_completion(
        self, model: str, messages: list[dict], stream: bool = False,
        temperature: float = 0.7, max_tokens: int | None = None, **kwargs,
    ) -> dict | AsyncGenerator:
        backend_model = model.replace("eden-local/", "")
        if not backend_model.endswith(".gguf"):
            backend_model += ".gguf"

        # Qwen3.5 thinking models require minimum 4096 tokens
        min_tokens = 4096
        if max_tokens is None or max_tokens < min_tokens:
            max_tokens = min_tokens

        # Strip params eden.cpp may not support (forwarded from Eden OE)
        payload = {"model": backend_model, "messages": messages, "stream": stream, "temperature": temperature, "max_tokens": max_tokens}

        if stream:
            return self._stream(payload)
        resp = await self._client.post(f"{self.base_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def _stream(self, payload: dict) -> AsyncGenerator:
        """Stream SSE chunks from eden.cpp.

        Qwen3.5 thinking models emit reasoning_content before content.
        The thinking phase can take 2-5s with no content tokens, causing
        Eden OE' idle timeout to fire. We forward reasoning_content as
        content so the client sees continuous data flow.
        """
        import json as _json
        async with self._client.stream("POST", f"{self.base_url}/v1/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                # Rewrite: if content is empty/null but reasoning_content exists,
                # move reasoning_content into content so downstream sees data
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        data = _json.loads(line[6:])
                        for choice in data.get("choices", []):
                            delta = choice.get("delta", {})
                            if not delta.get("content") and delta.get("reasoning_content"):
                                delta["content"] = delta.pop("reasoning_content")
                        line = "data: " + _json.dumps(data)
                    except Exception:
                        pass
                yield line + "\n\n"

    async def health_check(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False
