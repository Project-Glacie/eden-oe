"""
Eden API Gateway — Main Server

FastAPI server providing an OpenAI-compatible aggregate API.
Routes requests to the appropriate provider backend based on model name.

Endpoints:
  GET  /health              — Health check with backend status
  GET  /v1/models           — List all models across all providers
  POST /v1/chat/completions — Chat completion (OpenAI-compatible)
  GET  /v1/providers        — List available providers

Usage:
  uvicorn eden_gateway.server:app --host 127.0.0.1 --port 9100
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from eden_gateway.providers import get_registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("eden-gateway")

CONFIG = {
    "default_model": "deepseek/deepseek-v4-pro",
    "eden_local": {
        "base_url": "http://127.0.0.1:9093",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
    },
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: refresh model catalog from all providers."""
    log.info("Eden API Gateway starting...")
    registry = get_registry(CONFIG)
    await registry.refresh_models()
    log.info(f"Ready. Providers: {registry.provider_names}")
    yield
    log.info("Eden API Gateway shutting down.")


app = FastAPI(
    title="Eden API Gateway",
    description="Aggregate AI model router for Eden OE — OpenAI-compatible API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Health check with per-provider status."""
    registry = get_registry()
    statuses = {}
    for name, provider in registry._providers.items():
        try:
            healthy = await provider.health_check()
            statuses[name] = "healthy" if healthy else "unhealthy"
        except Exception as e:
            statuses[name] = f"error: {e}"

    all_healthy = all(s == "healthy" for s in statuses.values())
    return {
        "status": "ok" if all_healthy else "degraded",
        "providers": statuses,
        "models_loaded": len(registry._model_index),
    }


@app.get("/v1/providers")
async def list_providers():
    """List all registered providers and their status."""
    registry = get_registry()
    providers = []
    for name, provider in registry._providers.items():
        providers.append({
            "name": name,
            "display_name": provider.display_name,
            "healthy": await provider.health_check(),
        })
    return {"providers": providers}


@app.get("/v1/models")
async def list_models():
    """List all models across all providers (OpenAI-compatible format)."""
    registry = get_registry()
    await registry.refresh_models()

    data = []
    for model_id, provider in registry._model_index.items():
        data.append({
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": provider.name,
        })

    return {
        "object": "list",
        "data": data,
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions endpoint.

    Accepts standard OpenAI chat completion format.
    Routes to the appropriate provider based on model name.

    Supports:
      - Streaming (stream: true) via SSE
      - Non-streaming (stream: false or omitted)
    """
    body = await request.json()

    model_id = body.get("model", "")
    if not model_id:
        raise HTTPException(status_code=400, detail="model is required")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")

    stream = body.get("stream", False)
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens")

    registry = get_registry()
    resolved = registry.resolve(model_id)

    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{model_id}' not found. Available: /v1/models",
        )

    provider, backend_model = resolved
    log.info(f"Routing: {model_id} → {provider.name}/{backend_model} (stream={stream})")

    try:
        result = await provider.chat_completion(
            model=backend_model,
            messages=messages,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as e:
        log.error(f"Provider error ({provider.name}): {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Provider '{provider.name}' error: {str(e)}",
        )

    if stream:
        async def sse_wrapper():
            async for chunk in result:
                yield chunk
            yield "data: [DONE]\n\n"
        return StreamingResponse(sse_wrapper(), media_type="text/event-stream")
    else:
        return result


@app.post("/v1/keys")
async def store_key(request: Request):
    """Store an API key in chest.db.

    Body: {"provider": "deepseek", "api_key": "sk-..."}
    """
    from eden_gateway.auth import store_api_key

    body = await request.json()
    provider = body.get("provider")
    api_key = body.get("api_key")

    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="provider and api_key are required")

    success = store_api_key(provider, api_key)
    if success:
        return {"status": "ok", "provider": provider}
    else:
        raise HTTPException(status_code=500, detail="Failed to store API key")


# ─── CLI entry point ───────────────────────────────────────────
def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9100, log_level="info")


if __name__ == "__main__":
    main()
