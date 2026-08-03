"""
Eden API Gateway

Aggregate AI model router for Eden OE.
Provides an OpenAI-compatible API that routes to local and cloud providers.

Usage:
  eden-gateway serve        — Start the gateway on :9100
  eden-gateway health       — Check health of all backends
  eden-gateway models       — List all models across all providers
  eden-gateway key set <provider> <key>  — Store an API key in chest.db
"""
