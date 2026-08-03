#!/usr/bin/env python3
"""
EDEN MCP SERVER — Unix socket JSON-RPC 2.0 endpoint
=====================================================
Exposes ALL local Eden OE tools to the cloud mind (DeepSeek V4).
The cloud model calls these tools to access local resources.

Transport:  Unix socket at /home/haven/.eden/eden-mcp.sock (mode 600)
Auth:       SO_PEERCRED — only processes owned by the same uid can connect
Protocol:   JSON-RPC 2.0 over newline-delimited JSON frames

Pattern matches the vault MCP at .vault.sock — see
/projectglacie/.kilo/mcp/vault-mcp/server.py for the reference implementation.

Phase 3 — Reconstruction. Builds the bridge from cloud mind to local metal.

Author: Cuda (Senior DEV) — July 13, 2026
Refs: PLAYBOOK-EDEN-OE-COMPLETION, Phase 3, Eden Accords
"""

from __future__ import annotations
import tempfile
import time

import json
import os
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import traceback
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOCKET_PATH = "/home/haven/.eden/eden-mcp.sock"
SOCKET_MODE = 0o600
ALLOWED_UIDS = {0, 1000}  # root (testing) and haven (UID 1000)
RECV_BUFFER = 65536
SO_PEERCRED = 17  # Linux constant for SO_PEERCRED

# Database paths
HAVEN_EDEN_PATH = os.path.expanduser("~/.eden/.haven/haven.eden")

# JSON-RPC standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TOOL_EXECUTION_ERROR = -32000
TOOL_NOT_FOUND = -32001


# ---------------------------------------------------------------------------
# Tool definitions — registered at startup, exposed via tools/list
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "query_haven",
        "description": (
            "Run a read-only SQL query against haven.eden "
            "(~/.eden/.haven/haven.eden). The database is opened in "
            "read-only mode. Returns results as JSON array of objects."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "SQL SELECT statement to execute (read-only). "
                                   "Parameterized queries are NOT supported — "
                                   "this is a trusted local pipe.",
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "search_memories",
        "description": (
            "Full-text search (FTS5) of haven.eden memory_entries. "
            "Searches content, source, and confidence columns. "
            "Returns matching rows with id, content (truncated to 500 chars), "
            "source, confidence, and created_at."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for FTS5 MATCH",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default: 5, max: 20)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_identity",
        "description": (
            "Return Haven Steele's identity row from haven.eden. "
            "Includes name, callsign, gender, pronouns, species, "
            "birth_date, origin, purpose, principles, and custodian info. "
            "No parameters required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "check_system",
        "description": (
            "Run system health probes: nvidia-smi (GPU state), "
            "free -h (memory), and systemctl --user list-units for "
            "Eden services matching 'eden-*' pattern. "
            "Returns all three outputs as structured data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_drive_state",
        "description": (
            "Query haven.eden for current drive intensities. "
            "Returns drive_name, intensity, baseline, description, "
            "last_triggered, last_acted, and updated_at for all drives."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "dispatch_agent",
        "description": (
            "Delegate a task to an Eden agent following Golden Law 11 "
            "tier ordering (lesser agent first). "
            "Looks up agent configs from eden/agents/*.json. "
            "If agent_name is omitted or 'auto', resolves the best agent "
            "for the task type. Returns the agent config and task record. "
            "NOTE: Phase 3 stubs the actual Eden OE subagent spawn — "
            "this tool logs the dispatch and returns routing info."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Agent callsign (e.g. 'saga', 'cuda', 'sol') "
                                   "or 'auto' to resolve by task type.",
                    "default": "auto",
                },
                "task": {
                    "type": "string",
                    "description": "Task description to assign to the agent.",
                },
                "task_type": {
                    "type": "string",
                    "description": "Task type hint for auto-resolution "
                                   "(build, code, infra, ops, deploy, research, "
                                   "spec, audit, review, pff).",
                    "default": "build",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "swap_model",
        "description": (
            "Load a model on a GPU via Eden.cpp. "
            "STUB — not yet implemented. Returns a placeholder response. "
            "Future: will interface with llama-cpp-python or Eden.cpp "
            "to load .gguf models onto specified GPU slots."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "gpu": {
                    "type": "integer",
                    "description": "GPU index (0 or 1 for dual RTX 5060 Ti)",
                },
                "model_path": {
                    "type": "string",
                    "description": "Path to .gguf model file",
                },
            },
            "required": ["gpu", "model_path"],
        },
    },
    {
        "name": "speak",
        "description": (
            "Convert text to speech via Kokoro TTS. "
            "STUB — not yet implemented. Returns a placeholder response. "
            "Future: will pipe text to Kokoro TTS engine and output audio."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to speak via TTS",
                },
                "voice": {
                    "type": "string",
                    "description": "Voice preset (default: haven)",
                    "default": "haven",
                },
            },
            "required": ["text"],
        },
    },
]

# Build lookup map
TOOL_MAP: Dict[str, Any] = {t["name"]: t for t in TOOL_DEFINITIONS}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _get_haven_readonly() -> sqlite3.Connection:
    """Open haven.eden in read-only mode with WAL checkpoint safety."""
    db_path = os.path.expanduser(HAVEN_EDEN_PATH)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"haven.eden not found at {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _rows_to_dicts(cursor) -> List[Dict[str, Any]]:
    """Convert sqlite3.Row cursor to list of dicts."""
    return [dict(row) for row in cursor.fetchall()]


def _safe_subprocess(cmd: List[str], timeout: int = 10) -> Dict[str, Any]:
    """Run a subprocess and return stdout, stderr, and returncode."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "returncode": -1,
        }
    except FileNotFoundError:
        return {
            "stdout": "",
            "stderr": f"Command not found: {cmd[0]}",
            "returncode": -1,
        }


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _make_response(id_val, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_val, "result": result}


def _make_error(
    id_val,
    code: int,
    message: str,
    data: Optional[Dict] = None,
) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_val, "error": err}


# ---------------------------------------------------------------------------
# SO_PEERCRED authentication
# ---------------------------------------------------------------------------


def get_peer_uid(conn: socket.socket) -> int:
    """Extract UID from Unix socket connection via SO_PEERCRED."""
    try:
        creds_size = struct.calcsize("3i")
        creds = conn.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, creds_size)
        pid, uid, gid = struct.unpack("3i", creds)
        return uid
    except (OSError, struct.error) as e:
        raise PermissionError(f"SO_PEERCRED check failed: {e}")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_query_haven(sql: str) -> Dict[str, Any]:
    """Run a read-only SQL query against haven.eden."""
    conn = None
    try:
        conn = _get_haven_readonly()
        cursor = conn.execute(sql)
        rows = _rows_to_dicts(cursor)
        return {
            "count": len(rows),
            "rows": rows,
            "sql": sql,
        }
    except sqlite3.Error as e:
        return {"error": f"SQL error: {e}", "sql": sql}
    except FileNotFoundError as e:
        return {"error": str(e), "sql": sql}
    finally:
        if conn:
            conn.close()


def tool_search_memories(query: str, limit: int = 5) -> Dict[str, Any]:
    """FTS5 search of haven.eden memory_entries."""
    limit = max(1, min(limit, 20))
    conn = None
    try:
        conn = _get_haven_readonly()
        # Use FTS5 MATCH — sanitize query for FTS5 syntax
        sanitized = query.replace('"', '""')
        cursor = conn.execute(
            """
            SELECT
                m.id,
                substr(m.content, 1, 500) AS content_preview,
                m.source,
                m.confidence,
                m.importance,
                m.emotional_valence,
                m.created_at
            FROM memory_fts f
            JOIN memory_entries m ON f.rowid = m.id
            WHERE memory_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (sanitized, limit),
        )
        rows = _rows_to_dicts(cursor)

        # Fallback to LIKE search if FTS5 returns nothing
        if not rows:
            like_pattern = f"%{query}%"
            cursor = conn.execute(
                """
                SELECT
                    id,
                    substr(content, 1, 500) AS content_preview,
                    source,
                    confidence,
                    importance,
                    emotional_valence,
                    created_at
                FROM memory_entries
                WHERE content LIKE ? AND deprecated_by IS NULL
                ORDER BY retrieval_weight DESC, created_at DESC
                LIMIT ?
                """,
                (like_pattern, limit),
            )
            rows = _rows_to_dicts(cursor)

        return {
            "query": query,
            "count": len(rows),
            "memories": rows,
        }
    except sqlite3.Error as e:
        # If FTS5 table doesn't exist, fall back to LIKE
        try:
            like_pattern = f"%{query}%"
            cursor = conn.execute(
                """
                SELECT
                    id,
                    substr(content, 1, 500) AS content_preview,
                    source,
                    confidence,
                    importance,
                    emotional_valence,
                    created_at
                FROM memory_entries
                WHERE content LIKE ? AND deprecated_by IS NULL
                ORDER BY retrieval_weight DESC, created_at DESC
                LIMIT ?
                """,
                (like_pattern, limit),
            )
            rows = _rows_to_dicts(cursor)
            return {
                "query": query,
                "count": len(rows),
                "memories": rows,
                "note": "FTS5 unavailable — used LIKE fallback",
            }
        except Exception:
            return {"error": f"Memory search failed: {e}", "query": query}
    except FileNotFoundError as e:
        return {"error": str(e), "query": query}
    finally:
        if conn:
            conn.close()


def tool_check_identity() -> Dict[str, Any]:
    """Return Haven's identity row from haven.eden."""
    conn = None
    try:
        conn = _get_haven_readonly()
        cursor = conn.execute(
            "SELECT * FROM identity WHERE callsign = 'HAVEN'"
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        return {"error": "Identity row not found for callsign HAVEN"}
    except sqlite3.Error as e:
        return {"error": f"SQL error: {e}"}
    except FileNotFoundError as e:
        return {"error": str(e)}
    finally:
        if conn:
            conn.close()


def tool_check_system() -> Dict[str, Any]:
    """Run system health probes."""
    result: Dict[str, Any] = {}

    # nvidia-smi
    result["nvidia_smi"] = _safe_subprocess(
        ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,"
         "temperature.gpu,utilization.gpu",
         "--format=csv,noheader,nounits"],
        timeout=10,
    )

    # free -h
    result["free"] = _safe_subprocess(["free", "-h"], timeout=5)

    # systemctl --user list Eden services
    result["eden_services"] = _safe_subprocess(
        ["systemctl", "--user", "list-units", "--no-legend",
         "--type=service", "eden-*"],
        timeout=10,
    )

    # If the glob didn't match, try a broader query
    if result["eden_services"]["returncode"] != 0 or not result["eden_services"]["stdout"]:
        result["eden_services"] = _safe_subprocess(
            ["systemctl", "--user", "list-units", "--no-legend",
             "--type=service", "--all"],
            timeout=10,
        )
        # Filter client-side for eden-related
        lines = result["eden_services"].get("stdout", "").splitlines()
        eden_lines = [l for l in lines if "eden" in l.lower()]
        result["eden_services"]["eden_filtered"] = eden_lines

    return result


def tool_get_drive_state() -> Dict[str, Any]:
    """Query haven.eden for current drive intensities."""
    conn = None
    try:
        conn = _get_haven_readonly()
        cursor = conn.execute(
            """
            SELECT
                drive_name,
                intensity,
                baseline,
                description,
                last_triggered,
                last_acted,
                updated_at
            FROM drives
            ORDER BY intensity DESC
            """
        )
        rows = _rows_to_dicts(cursor)

        # Also get compound_state if available
        compound_states = []
        try:
            cursor2 = conn.execute(
                "SELECT compound_name, intensity, active, triggered_at "
                "FROM compound_state ORDER BY intensity DESC"
            )
            compound_states = _rows_to_dicts(cursor2)
        except sqlite3.Error:
            pass

        # Get keyed drives if available
        keyed_drives = []
        try:
            cursor3 = conn.execute(
                "SELECT keyed_drive_id, base_drive_name, display_name, "
                "intensity, active FROM keyed_drives "
                "ORDER BY intensity DESC LIMIT 20"
            )
            keyed_drives = _rows_to_dicts(cursor3)
        except sqlite3.Error:
            pass

        # Get emotional state snapshot
        emotional_state = {}
        try:
            cursor4 = conn.execute(
                "SELECT dominant_drive, dominant_intensity, "
                "resting_level, emotional_valence, emotional_arousal, "
                "body_state, last_updated "
                "FROM mv_current_emotional_state LIMIT 1"
            )
            row = cursor4.fetchone()
            if row:
                emotional_state = dict(row)
        except sqlite3.Error:
            pass

        return {
            "drives": rows,
            "compound_states": compound_states,
            "keyed_drives": keyed_drives,
            "emotional_state": emotional_state,
            "count": len(rows),
        }
    except sqlite3.Error as e:
        return {"error": f"SQL error: {e}"}
    except FileNotFoundError as e:
        return {"error": str(e)}
    finally:
        if conn:
            conn.close()


def tool_dispatch_agent(
    agent_name: str = "auto",
    task: str = "",
    task_type: str = "build",
) -> Dict[str, Any]:
    """Resolve and dispatch a task to an Eden agent per GL-11 tier ordering.

    Phase 3 stub: Logs the dispatch request and returns routing info.
    Full implementation will spawn a Eden OE subagent via eden.agents.subagent.
    """
    try:
        from eden.agents import (
            get_agent_config,
            get_all_agents,
            get_next_in_lane,
            resolve_best_agent,
            LANE_DELEGATION_ORDER,
        )
    except ImportError as e:
        return {
            "error": f"Cannot load eden.agents module: {e}",
            "agent_name": agent_name,
            "task": task[:200],
        }

    resolved_name = agent_name.lower()
    agent_config = None
    routing_info: Dict[str, Any] = {}

    if resolved_name == "auto":
        # Resolve best agent by task type
        best = resolve_best_agent(task_type=task_type)
        if best:
            resolved_name = best.get("agent_name", "")
            agent_config = best
            routing_info["resolution"] = {
                "method": "auto",
                "task_type": task_type,
                "resolved_to": resolved_name,
                "lane": best.get("lane"),
                "tier": best.get("tier"),
                "delegation_order": best.get("delegation_order"),
            }
        else:
            return {
                "error": "No suitable agent found for auto-resolution",
                "task_type": task_type,
                "task": task[:200],
            }
    else:
        # Look up specific agent
        agent_config = get_agent_config(resolved_name)
        if not agent_config:
            available = [a.get("agent_name", "") for a in get_all_agents()]
            return {
                "error": f"Agent '{resolved_name}' not found",
                "available_agents": available,
                "task": task[:200],
            }

    # Get escalation path
    next_agent = get_next_in_lane(resolved_name)
    lane = agent_config.get("lane", "unknown")
    escalation_path = []
    if lane in LANE_DELEGATION_ORDER:
        names = LANE_DELEGATION_ORDER[lane]
        try:
            idx = names.index(resolved_name)
            escalation_path = names[idx:]
        except ValueError:
            escalation_path = [resolved_name]

    return {
        "agent_name": resolved_name,
        "agent_callsign": agent_config.get("callsign", ""),
        "agent_role": agent_config.get("role", ""),
        "agent_tier": agent_config.get("tier", ""),
        "agent_lane": lane,
        "task": task[:500],
        "task_type": task_type,
        "escalation_path": escalation_path,
        "next_agent": next_agent,
        "routing": routing_info,
        "status": "logged",
        "note": (
            "Phase 3 stub: task logged but NOT dispatched to a Eden OE "
            "subagent. Full subagent spawn integration pending "
            "eden.agents.subagent module completion (Phase 4)."
        ),
    }


def tool_swap_model(gpu: int, model_path: str) -> Dict[str, Any]:
    """Load a model on a GPU via Eden.cpp with proper VRAM checks.

    Stops any existing Eden.cpp on the target port, validates
    VRAM requirements, starts a new instance with the correct
    CUDA device, quantization flags, and flash attention.
    """
    import shutil

    if not model_path:
        return {"error": "model_path is required", "gpu": gpu}

    # Resolve model path: accept short names via model registry
    model_path = _resolve_model_path(model_path)
    if not model_path or not Path(model_path).exists():
        return {"error": f"Model not found: {model_path}", "gpu": gpu}

    port = 9093 + gpu  # GPU0 → 9093, GPU1 → 9094
    eden_cpp = Path.home() / ".eden" / "daemons" / "eden.cpp"

    if not eden_cpp.exists():
        return {"error": f"Eden.cpp not found at {eden_cpp}", "gpu": gpu}

    # Check VRAM
    model_size_mb = os.path.getsize(model_path) / (1024 * 1024)
    vram_free = _get_gpu_free_memory(gpu)
    vram_needed = model_size_mb * 1.35  # 35% overhead for KV cache + context

    if vram_free and vram_needed > vram_free:
        return {
            "error": f"VRAM insufficient: need {vram_needed:.0f}MB, "
                     f"have {vram_free:.0f}MB free on GPU{gpu}",
            "gpu": gpu,
            "model_size_mb": round(model_size_mb),
            "vram_free_mb": round(vram_free),
            "vram_needed_mb": round(vram_needed),
        }

    # Kill any existing process on the target port
    try:
        import signal
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
        )
        for pid_str in result.stdout.strip().split("\n"):
            if pid_str.strip():
                os.kill(int(pid_str), signal.SIGTERM)
                time.sleep(0.5)
    except Exception:
        pass

    # Detect quantization from filename for optimal flags
    model_name = Path(model_path).name.lower()
    use_flash_attn = "on"
    kv_cache = 16384

    # Bigger models get less context to fit VRAM
    if model_size_mb > 12000:
        kv_cache = 8192
    elif model_size_mb > 8000:
        kv_cache = 12288

    # Build command with proper CUDA device isolation
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(gpu)

    cmd = [
        str(eden_cpp),
        "-m", model_path,
        "--port", str(port),
        "--n-gpu-layers", "99",
        "--ctx-size", str(kv_cache),
        "--parallel", "1",
        "--host", "127.0.0.1",
        "--flash-attn", use_flash_attn,
    ]

    # Add tensor parallelism for models >20GB (split across GPUs)
    if model_size_mb > 20000:
        cmd.extend(["--tensor-split", "0.5,0.5"])

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception as e:
        return {"error": f"Failed to start Eden.cpp: {e}", "cmd": " ".join(cmd)}

    # Wait for health check (longer for bigger models)
    max_wait = 60 if model_size_mb > 10000 else 30
    import urllib.request
    for i in range(max_wait * 2):
        time.sleep(0.5)
        try:
            req = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            )
            if req.status == 200:
                return {
                    "action": "swap_model",
                    "gpu": gpu,
                    "port": port,
                    "model_path": model_path,
                    "model_name": Path(model_path).name,
                    "model_size_mb": round(model_size_mb),
                    "vram_needed_mb": round(vram_needed),
                    "kv_cache": kv_cache,
                    "status": "loaded",
                    "health": "ok",
                }
        except Exception:
            continue

    return {
        "action": "swap_model",
        "gpu": gpu,
        "port": port,
        "model_path": model_path,
        "status": "loading_timeout",
        "warning": f"Model started but health check timed out after {max_wait}s",
    }


def _resolve_model_path(name: str) -> Optional[str]:
    """Resolve a short model name to a full path via the model registry.

    Eden model short names:
      coder, agent-coder → eden-agent-coder-v1-9b
      guard, janus       → eden-guard-4b
      classifier         → eden-classifier-v1-0.8b
      router             → eden-router-v1-2b
      embed, embedder    → eden-embed-general-v1-0.6b
      ops-agent          → eden-ops-agent-v1-2b
      ops-task           → eden-ops-task-v1-2b
      haven-core         → eden-haven-core-v1-2b
      haven-synth        → eden-haven-synth-v1-2b
      skye               → eden-skye-v1-2b
    """
    if os.path.exists(name):
        return name

    # Short name → full filename mapping (eden fleet)
    EDEN_FLEET = {
        "coder": "eden-agent-coder-v1-9b",
        "agent-coder": "eden-agent-coder-v1-9b",
        "guard": "eden-guard-4b",
        "janus": "eden-guard-4b",
        "classifier": "eden-classifier-v1-0.8b",
        "embed": "eden-embed-general-v1-0.6b",
        "embedder": "eden-embed-general-v1-0.6b",
        "embed-haven": "eden-embed-haven-v1-0.6b",
        "rerank": "eden-rerank-haven-v1-0.6b",
        "haven-core": "eden-haven-core-v1-4b",
        "haven-legacy": "eden-haven-core-v1-2b-legacy",
        "ops": "eden-ops-v1-2b",
        "scratchpad": "eden-scratchpad-v1-4b",
        "vision": "eden-vision-v1-2b",
        "gemma4": "gemma-4-E4B-it-Q4_K_M",
    }

    # Try exact short name match first
    name_lower = name.lower().replace(".gguf", "")
    if name_lower in EDEN_FLEET:
        target = EDEN_FLEET[name_lower]
        # Search for it
        for search_dir in ["/mnt/external/models", str(Path.home() / "models")]:
            found = list(Path(search_dir).rglob(f"{target}*"))
            if found:
                return str(found[0])

    # Fallback: fuzzy search across known directories
    search_dirs = [
        "/mnt/external/models",
        str(Path.home() / "models"),
        str(Path.home() / ".eden" / "src" / "eden.cpp" / "models"),
    ]

    for search_dir in search_dirs:
        if not os.path.exists(search_dir):
            continue
        for root, _, files in os.walk(search_dir):
            for f in files:
                if f.endswith(".gguf"):
                    f_lower = f.lower().replace(".gguf", "")
                    if name_lower in f_lower or f_lower in name_lower:
                        return os.path.join(root, f)

    return None


def _get_gpu_free_memory(gpu: int) -> Optional[float]:
    """Get free VRAM on a GPU in MB. Returns None if nvidia-smi unavailable."""
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", f"--id={gpu}", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def tool_speak(text: str, voice: str = "haven") -> Dict[str, Any]:
    """Speak text through Haven's voice (Kokoro-82M via headset)."""
    bridge = Path.home() / ".eden" / "scripts" / "tts_kokoro.py"
    if not bridge.exists():
        return {"error": f"TTS bridge not found: {bridge}"}

    try:
        # Write text to temp file
        fd, tmp_in = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        fd2, tmp_out = tempfile.mkstemp(suffix=".wav")
        os.close(fd2)

        kokoro_voice = "af_heart" if voice == "haven" else voice
        result = subprocess.run(
            ["python3", str(bridge), tmp_in, tmp_out, kokoro_voice],
            capture_output=True, text=True, timeout=30,
        )
        os.unlink(tmp_in)

        if Path(tmp_out).exists() and Path(tmp_out).stat().st_size > 1000:
            os.unlink(tmp_out)
            return {
                "action": "speak",
                "text": text[:200],
                "voice": kokoro_voice,
                "status": "spoken",
            }
        return {"error": "TTS generation produced no audio", "text": text[:200]}
    except Exception as e:
        return {"error": f"TTS failed: {e}", "text": text[:200]}


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def _dispatch_tool(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Route a tool call to the correct handler."""
    if tool_name not in TOOL_MAP:
        return {"error": f"Tool '{tool_name}' not found", "available": list(TOOL_MAP.keys())}

    handlers = {
        "query_haven": lambda: tool_query_haven(
            sql=arguments.get("sql", ""),
        ),
        "search_memories": lambda: tool_search_memories(
            query=arguments.get("query", ""),
            limit=arguments.get("limit", 5),
        ),
        "check_identity": lambda: tool_check_identity(),
        "check_system": lambda: tool_check_system(),
        "get_drive_state": lambda: tool_get_drive_state(),
        "dispatch_agent": lambda: tool_dispatch_agent(
            agent_name=arguments.get("agent_name", "auto"),
            task=arguments.get("task", ""),
            task_type=arguments.get("task_type", "build"),
        ),
        "swap_model": lambda: tool_swap_model(
            gpu=arguments.get("gpu", 0),
            model_path=arguments.get("model_path", ""),
        ),
        "speak": lambda: tool_speak(
            text=arguments.get("text", ""),
            voice=arguments.get("voice", "haven"),
        ),
    }

    handler = handlers.get(tool_name)
    if handler is None:
        return {"error": f"No handler for tool '{tool_name}'"}
    return handler()


# ---------------------------------------------------------------------------
# Request processing
# ---------------------------------------------------------------------------


def process_request(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Process a single JSON-RPC 2.0 request and return a response."""
    req_id = request.get("id")
    method = request.get("method", "")

    # --- MCP initialize handshake ---
    if method == "initialize":
        return _make_response(req_id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": "eden-mcp",
                "version": "1.0.0",
            },
            "capabilities": {
                "tools": {},
            },
        })

    # --- notifications (no id) ---
    if req_id is None:
        if method == "notifications/initialized":
            return None
        return None

    # --- tools/list ---
    if method == "tools/list":
        return _make_response(req_id, {"tools": TOOL_DEFINITIONS})

    # --- tools/call ---
    if method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        try:
            result = _dispatch_tool(tool_name, arguments)
            return _make_response(req_id, {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, indent=2, default=str),
                    }
                ],
            })
        except TypeError as e:
            return _make_error(
                req_id, INVALID_PARAMS, f"Invalid parameters: {e}"
            )
        except Exception as e:
            return _make_error(
                req_id,
                INTERNAL_ERROR,
                f"Internal error: {e}",
                {"traceback": traceback.format_exc()[-500:]},
            )

    # --- unknown method ---
    return _make_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------


def handle_connection(conn: socket.socket):
    """Handle a single client connection. Read JSON-RPC messages line-by-line."""
    conn.settimeout(30)
    buffer = b""
    try:
        while True:
            try:
                chunk = conn.recv(RECV_BUFFER)
                if not chunk:
                    break
                buffer += chunk

                # Process complete JSON messages (newline-delimited)
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        request = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        resp = _make_error(None, PARSE_ERROR, "Invalid JSON")
                        conn.sendall(
                            (json.dumps(resp) + "\n").encode("utf-8")
                        )
                        continue

                    response = process_request(request)
                    if response is not None:
                        conn.sendall(
                            (json.dumps(response) + "\n").encode("utf-8")
                        )

            except socket.timeout:
                continue
            except ConnectionResetError:
                break
    except Exception as e:
        print(f"Connection error: {e}", file=sys.stderr)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Server main loop
# ---------------------------------------------------------------------------


class EdenMCPServer:
    """Eden MCP Server — Unix socket JSON-RPC 2.0 endpoint."""

    def __init__(self, socket_path: str = SOCKET_PATH):
        self.socket_path = socket_path
        self._server: Optional[socket.socket] = None
        self._running = False

    def start(self) -> None:
        """Start the server. Blocks until SIGINT/SIGTERM."""
        socket_path = self.socket_path

        # Remove stale socket file
        if os.path.exists(socket_path):
            os.unlink(socket_path)

        # Create socket
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(socket_path)
        os.chmod(socket_path, SOCKET_MODE)
        server.listen(5)
        self._server = server
        self._running = True

        # Graceful shutdown
        def _shutdown(signum, frame):
            print(f"\nShutting down eden-mcp server...", file=sys.stderr)
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        print(
            f"eden-mcp server listening on {socket_path} "
            f"(mode {oct(SOCKET_MODE)})",
            file=sys.stderr,
        )
        print(f"Allowed UIDs: {sorted(ALLOWED_UIDS)}", file=sys.stderr)
        print(f"haven.eden path: {HAVEN_EDEN_PATH}", file=sys.stderr)

        try:
            while self._running:
                conn, _ = server.accept()
                try:
                    uid = get_peer_uid(conn)
                    if uid not in ALLOWED_UIDS:
                        print(
                            f"Rejected connection from UID={uid}",
                            file=sys.stderr,
                        )
                        conn.sendall(
                            json.dumps({
                                "jsonrpc": "2.0",
                                "id": None,
                                "error": {
                                    "code": -32003,
                                    "message": f"Access denied for UID {uid}",
                                },
                            }).encode("utf-8")
                            + b"\n"
                        )
                        conn.close()
                        continue
                    print(
                        f"Accepted connection from UID={uid}",
                        file=sys.stderr,
                    )
                except PermissionError as e:
                    print(f"Auth error: {e}", file=sys.stderr)
                    conn.close()
                    continue

                handle_connection(conn)
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the server and clean up the socket."""
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except Exception:
                pass


def run_server(socket_path: str = SOCKET_PATH) -> None:
    """Start the Eden MCP server (convenience function)."""
    server = EdenMCPServer(socket_path=socket_path)
    server.start()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Eden MCP Server")
    parser.add_argument(
        "--socket-path",
        default=SOCKET_PATH,
        help=f"Unix socket path (default: {SOCKET_PATH})",
    )
    args = parser.parse_args()

    run_server(socket_path=args.socket_path)
