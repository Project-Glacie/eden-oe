# Langfuse Observability Plugin

This plugin ships bundled with Eden OE but is **opt-in** — it only loads when
you explicitly enable it.

## Enable

Pick one:

```bash
# Interactive: walks you through credentials + SDK install + enable
eden tools  # → Langfuse Observability

# Manual
pip install langfuse
eden plugins enable observability/langfuse
```

## Required credentials

Set these in `~/.eden/.env` (or via `eden tools`):

```bash
EDEN_OE_LANGFUSE_PUBLIC_KEY=pk-lf-...
EDEN_OE_LANGFUSE_SECRET_KEY=sk-lf-...
EDEN_OE_LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL
```

Without the SDK or credentials the hooks no-op silently — the plugin fails
open.

## Verify

```bash
eden plugins list                 # observability/langfuse should show "enabled"
eden chat -q "hello"              # then check Langfuse for a "Eden OE turn" trace
```

## Optional tuning

```bash
EDEN_OE_LANGFUSE_ENV=production       # environment tag
EDEN_OE_LANGFUSE_RELEASE=v1.0.0       # release tag
EDEN_OE_LANGFUSE_SAMPLE_RATE=0.5      # sample 50% of traces
EDEN_OE_LANGFUSE_MAX_CHARS=12000      # max chars per field (default: 12000)
EDEN_OE_LANGFUSE_DEBUG=true           # verbose plugin logging
```

## Disable

```bash
eden plugins disable observability/langfuse
```
