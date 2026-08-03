"""Eden MCP Server — Unix socket JSON-RPC 2.0 endpoint.

Exposes local Eden OE tools to the cloud mind (DeepSeek V4) via a
Unix domain socket with SO_PEERCRED authentication. No API keys needed
— auth is kernel-enforced.

Tools:
    query_haven     — Read-only SQL against haven.eden
    search_memories — FTS5 search of memory_entries
    check_identity  — Return Haven's identity row
    check_system    — nvidia-smi, free -h, systemctl Eden services
    get_drive_state — Current drive intensities
    dispatch_agent  — Delegate to Eden agent per GL-11 tier ordering
    swap_model      — Load model on GPU via Eden.cpp (stub)
    speak           — Kokoro TTS (stub)

Transport:  Unix socket at /home/haven/.eden/eden-mcp.sock (mode 600)
Auth:       SO_PEERCRED — only same-uid processes can connect
Protocol:   JSON-RPC 2.0 over newline-delimited JSON frames
"""

from eden.mcp.server import EdenMCPServer, run_server

__all__ = ["EdenMCPServer", "run_server"]
