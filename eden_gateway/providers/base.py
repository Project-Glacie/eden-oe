"""
Eden API Gateway — Provider Base Class

All gateway providers must implement this interface.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator


@dataclass
class GatewayModel:
    """A model offered by a provider backend."""
    id: str
    provider: str
    context_length: int = 4096
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    capabilities: list[str] = field(default_factory=lambda: ["chat"])
    description: str = ""


class BaseProvider(ABC):
    """Abstract provider interface for the Eden API Gateway."""

    name: str = "base"
    display_name: str = "Base Provider"
    base_url: str = ""

    @abstractmethod
    async def list_models(self) -> list[GatewayModel]:
        """Return all models available from this provider."""
        ...

    @abstractmethod
    async def chat_completion(
        self,
        model: str,
        messages: list[dict],
        stream: bool = False,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs,
    ) -> dict | AsyncGenerator:
        """Send a chat completion request.

        Returns:
            dict for non-streaming, AsyncGenerator[str] for streaming (SSE chunks)
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the provider backend is reachable and healthy."""
        ...

    def get_auth_headers(self) -> dict:
        """Return auth headers for API requests. Override per provider."""
        return {}
