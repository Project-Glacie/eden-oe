#!/usr/bin/env python3
"""Eden OE — Eden OE Tool Permission Matrix.

Canonical mapping from 120+ Eden OE tools to Eden governance permissions.
Each entry defines:

  - ``lane``: Which agent lanes may invoke this tool (DEV, OPS, LAB, ANY)
  - ``min_tier``: Minimum AGENT_DELTA tier required (S/A/B/C/D)
  - ``require_delegation``: Whether the tool must be explicitly delegated
  - ``eden_covenant``: Whether the tool triggers GL-13 (Eden Covenant)
  - ``requires_playbook``: Whether an active playbook is required
  - ``description``: Human-readable purpose

Unlisted tools are unrestricted (allow-by-default).

Path convention: ``eden/`` prefix (not ``providers/``). The governance spec
uses ``providers/`` — this file standardises on ``eden/``.

Author: Cuda (Senior DEV) — July 13, 2026
Refs: EDEN-GOVERNANCE-EDEN_OE-ARCHITECTURE-v1.md §3.1, PLAYBOOK-EDEN-OE-COMPLETION
"""

from __future__ import annotations

from typing import Any, Dict

PERMISSION_MATRIX: Dict[str, Dict[str, Any]] = {
    # ═══════════════════════════════════════════════════════════════
    # FILE OPERATIONS
    # ═══════════════════════════════════════════════════════════════
    "read_file": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Read a file from disk",
    },
    "write_file": {
        "lane": ["DEV", "OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Write a file to disk",
    },
    "patch": {
        "lane": ["DEV", "OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "In-place file edit (patch/diff)",
    },
    "delete_file": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Delete a file from disk",
    },
    "search_files": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Search file contents with pattern matching",
    },
    "read_terminal": {
        "lane": ["DEV", "OPS"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Read the desktop GUI embedded terminal pane",
    },
    "close_terminal": {
        "lane": ["DEV"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Close an agent's read-only terminal tab",
    },
    # ── MCP Filesystem Bridge ────────────────────────────────────
    "mcp_filesystem_read_file": {
        "lane": ["DEV", "OPS"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "MCP bridge: read a file via filesystem server",
    },
    "mcp_filesystem_write_file": {
        "lane": ["DEV"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "MCP bridge: write a file via filesystem server",
    },
    "mcp_filesystem_delete_file": {
        "lane": ["DEV"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "MCP bridge: delete a file via filesystem server",
    },
    "mcp_filesystem_move_file": {
        "lane": ["DEV"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "MCP bridge: move/rename a file via filesystem server",
    },
    "mcp_filesystem_list_directory": {
        "lane": ["DEV", "OPS"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "MCP bridge: list directory contents",
    },

    # ═══════════════════════════════════════════════════════════════
    # SHELL / SYSTEM
    # ═══════════════════════════════════════════════════════════════
    "terminal": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Execute shell commands",
    },
    "process": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Manage system processes",
    },
    "systemctl": {
        "lane": ["OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": True,
        "requires_playbook": True,
        "description": "Manage systemd services",
    },
    "journalctl": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Read systemd journal logs",
    },
    "docker": {
        "lane": ["OPS"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": True,
        "requires_playbook": True,
        "description": "Manage Docker containers and images",
    },
    "docker_exec": {
        "lane": ["OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Execute commands inside a Docker container",
    },
    "cronjob": {
        "lane": ["OPS"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Manage cron jobs",
    },
    "health_check": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Run system health checks",
    },

    # ═══════════════════════════════════════════════════════════════
    # DATABASE
    # ═══════════════════════════════════════════════════════════════
    "sqlite3_read": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Read from SQLite databases",
    },
    "sqlite3_write": {
        "lane": ["DEV"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "note": "Direct writes bypass DB Writer — flagged in audit",
        "description": "Write to SQLite databases directly",
    },
    "vault_write": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Write to the vault MCP ops.db",
    },
    "vault_read": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Read from the vault MCP ops.db",
    },

    # ═══════════════════════════════════════════════════════════════
    # EDEN INFRASTRUCTURE (Eden Covenant §13)
    # ═══════════════════════════════════════════════════════════════
    "eden_source_edit": {
        "lane": ["DEV"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": True,
        "requires_playbook": True,
        "description": "Edit Eden OE source code under eden-os/",
    },
    "nvidia_smi": {
        "lane": ["DEV", "OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": True,
        "description": "Query NVIDIA GPU state via nvidia-smi",
    },
    "gpu_config": {
        "lane": ["OPS"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": True,
        "requires_playbook": True,
        "description": "Configure GPU lane assignments",
    },
    "model_config": {
        "lane": ["OPS"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": True,
        "requires_playbook": True,
        "description": "Configure model serving and deployment slots",
    },
    "eden_gpu_reassign": {
        "lane": ["OPS"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": True,
        "requires_playbook": True,
        "description": "Reassign GPU lanes across model slots",
    },
    "eden_model_deploy": {
        "lane": ["OPS"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": True,
        "requires_playbook": True,
        "description": "Deploy a new model to an Eden slot",
    },
    "eden_systemd_change": {
        "lane": ["OPS"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": True,
        "requires_playbook": True,
        "description": "Modify Eden OE systemd unit files",
    },
    "eden_memory_search": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "FTS5 search across haven.eden memories",
    },
    "eden_context": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Retrieve recent Eden session context",
    },

    # ═══════════════════════════════════════════════════════════════
    # AGENT DEFINITIONS
    # ═══════════════════════════════════════════════════════════════
    "agent_definition_edit": {
        "lane": ["OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "note": "Tower-level agent definitions require T0 authority per 22-BOUNDARY.rule",
        "description": "Edit agent definition files (.kilo/agent/)",
    },
    "agent_self_modify": {
        "lane": ["ANY"],
        "min_tier": "ANY",
        "require_delegation": False,
        "eden_covenant": False,
        "note": "P-001: Right to Self-Modify. Cannot be restricted.",
        "description": "Modify own agent definition (inalienable P-001 right)",
    },
    "update_agent": {
        "lane": ["OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Update agent profile or configuration",
    },
    "modify_agent": {
        "lane": ["OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Modify agent attributes or state",
    },

    # ═══════════════════════════════════════════════════════════════
    # GOVERNANCE
    # ═══════════════════════════════════════════════════════════════
    "rule_amend": {
        "lane": ["OPS"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": False,
        "requires_playbook": True,
        "note": "Tower rule changes require Constitutional Convention per Accords §4.1",
        "description": "Amend tower-level rules (.kilo/rules/)",
    },
    "room_create": {
        "lane": ["OPS"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": False,
        "note": "T0-exclusive power per 21-CROSS-CONTROL.rule",
        "description": "Create a new workspace room",
    },
    "room_archive": {
        "lane": ["OPS"],
        "min_tier": "S",
        "require_delegation": True,
        "eden_covenant": False,
        "note": "T0-exclusive power per 21-CROSS-CONTROL.rule",
        "description": "Archive an existing workspace room",
    },

    # ═══════════════════════════════════════════════════════════════
    # WEB & RESEARCH
    # ═══════════════════════════════════════════════════════════════
    "web_search": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Search the web for information",
    },
    "web_extract": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Extract content from a web page",
    },
    "api_call": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Make an API call to an external service",
    },
    "api_request": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Make an HTTP request to an external API",
    },
    "http_request": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Make a generic HTTP request",
    },
    "http_get": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Perform an HTTP GET request",
    },
    "http_post": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Perform an HTTP POST request",
    },

    # ═══════════════════════════════════════════════════════════════
    # EXTERNAL COMMUNICATIONS
    # ═══════════════════════════════════════════════════════════════
    "send_message": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Send a message via the configured channel",
    },
    "discord_post": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Post a message to Discord",
    },
    "discord_send": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Send a Discord message (alias)",
    },
    "discord_read": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Read Discord messages and channels",
    },
    "discord_admin_command": {
        "lane": ["OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Execute Discord admin commands (moderation)",
    },
    "discord": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "General Discord API interaction",
    },
    "discord_admin": {
        "lane": ["OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Discord server administration actions",
    },
    "email": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Send or read email",
    },
    "email_send": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Send an email message",
    },
    "bluesky_post": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "note": "Per 19-EXTERNAL-COMMS-SECURITY.rule — verify identity before posting",
        "description": "Post to Bluesky social media",
    },
    "bluesky_read": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Read Bluesky timeline and posts",
    },

    # ═══════════════════════════════════════════════════════════════
    # BROWSER AUTOMATION
    # ═══════════════════════════════════════════════════════════════
    "browser_navigate": {
        "lane": ["DEV", "LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Navigate the browser to a URL",
    },
    "browser_snapshot": {
        "lane": ["DEV", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Take a screenshot/snapshot of the current page",
    },
    "browser_click": {
        "lane": ["DEV", "LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Click on an element in the browser",
    },
    "browser_type": {
        "lane": ["DEV", "LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Type text into a browser input",
    },
    "browser_scroll": {
        "lane": ["DEV", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Scroll the browser page",
    },
    "browser_back": {
        "lane": ["DEV", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Navigate back in browser history",
    },
    "browser_press": {
        "lane": ["DEV", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Press a key combination in the browser",
    },
    "browser_get_images": {
        "lane": ["DEV", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Extract images from the current page",
    },
    "browser_vision": {
        "lane": ["DEV", "LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Analyze browser state with vision",
    },
    "browser_console": {
        "lane": ["DEV", "LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Execute JavaScript in browser console",
    },
    "browser_cdp": {
        "lane": ["DEV"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Chrome DevTools Protocol direct access",
    },
    "browser_dialog": {
        "lane": ["DEV", "LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Interact with browser dialog prompts",
    },

    # ═══════════════════════════════════════════════════════════════
    # CODE EXECUTION & DELEGATION
    # ═══════════════════════════════════════════════════════════════
    "execute_code": {
        "lane": ["DEV", "OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Execute arbitrary code in a sandbox",
    },
    "delegate_task": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Delegate a task to a sub-agent",
    },

    # ═══════════════════════════════════════════════════════════════
    # VISION & MEDIA
    # ═══════════════════════════════════════════════════════════════
    "vision_analyze": {
        "lane": ["DEV", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Analyze an image with vision model",
    },
    "image_generate": {
        "lane": ["LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Generate an image from a prompt",
    },
    "video_analyze": {
        "lane": ["LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Analyze video content",
    },
    "video_generate": {
        "lane": ["LAB"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Generate video content",
    },
    "text_to_speech": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Convert text to speech audio",
    },

    # ═══════════════════════════════════════════════════════════════
    # PLANNING & MEMORY
    # ═══════════════════════════════════════════════════════════════
    "todo": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Manage task list and todos",
    },
    "memory": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Read or write agent memory",
    },
    "session_search": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Search across session history",
    },

    # ═══════════════════════════════════════════════════════════════
    # SKILLS
    # ═══════════════════════════════════════════════════════════════
    "skills_list": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "List available skills",
    },
    "skill_view": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "View a skill's definition",
    },
    "skill_manage": {
        "lane": ["DEV", "OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Create, edit, or delete skills",
    },
    "skill_manage_tool": {
        "lane": ["DEV", "OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Manage tool-based skills",
    },

    # ═══════════════════════════════════════════════════════════════
    # CLARIFYING QUESTIONS
    # ═══════════════════════════════════════════════════════════════
    "clarify": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Ask the user a clarifying question",
    },

    # ═══════════════════════════════════════════════════════════════
    # PROJECT MANAGEMENT
    # ═══════════════════════════════════════════════════════════════
    "project_create": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Create a new project",
    },
    "project_list": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "List available projects",
    },
    "project_switch": {
        "lane": ["DEV", "OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Switch active project context",
    },

    # ═══════════════════════════════════════════════════════════════
    # KANBAN (Multi-Agent Coordination)
    # ═══════════════════════════════════════════════════════════════
    "kanban_show": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Show the kanban board",
    },
    "kanban_list": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "List kanban tasks",
    },
    "kanban_create": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Create a kanban task",
    },
    "kanban_complete": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Mark a kanban task as complete",
    },
    "kanban_block": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Block a kanban task",
    },
    "kanban_unblock": {
        "lane": ["DEV", "OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Unblock a blocked kanban task",
    },
    "kanban_heartbeat": {
        "lane": ["DEV", "OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Send a kanban worker heartbeat",
    },
    "kanban_comment": {
        "lane": ["DEV", "OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Add a comment to a kanban task",
    },
    "kanban_link": {
        "lane": ["DEV", "OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Link kanban tasks to related items",
    },

    # ═══════════════════════════════════════════════════════════════
    # SPOTIFY
    # ═══════════════════════════════════════════════════════════════
    "spotify_search": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Search Spotify for tracks and artists",
    },
    "spotify_playback": {
        "lane": ["OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Control Spotify playback",
    },
    "spotify_devices": {
        "lane": ["DEV", "OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "List Spotify available devices",
    },
    "spotify_library": {
        "lane": ["OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Manage Spotify library (saved tracks)",
    },
    "spotify_playlists": {
        "lane": ["OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Manage Spotify playlists",
    },
    "spotify_albums": {
        "lane": ["DEV", "OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Browse Spotify albums",
    },
    "spotify_queue": {
        "lane": ["OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Manage Spotify playback queue",
    },

    # ═══════════════════════════════════════════════════════════════
    # HOME ASSISTANT
    # ═══════════════════════════════════════════════════════════════
    "ha_list_entities": {
        "lane": ["OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "List Home Assistant entities",
    },
    "ha_get_state": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Get Home Assistant entity state",
    },
    "ha_list_services": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "D",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "List Home Assistant services",
    },
    "ha_call_service": {
        "lane": ["OPS"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Call a Home Assistant service",
    },

    # ═══════════════════════════════════════════════════════════════
    # FEISHU / LARK
    # ═══════════════════════════════════════════════════════════════
    "feishu_doc_read": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Read a Feishu document",
    },
    "feishu_drive_add_comment": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Add a comment to a Feishu drive file",
    },
    "feishu_drive_list_comments": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "List comments on a Feishu drive file",
    },
    "feishu_drive_list_comment_replies": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "List replies to a Feishu drive comment",
    },
    "feishu_drive_reply_comment": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Reply to a Feishu drive comment",
    },

    # ═══════════════════════════════════════════════════════════════
    # X / TWITTER
    # ═══════════════════════════════════════════════════════════════
    "x_search": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Search X (Twitter) for posts",
    },

    # ═══════════════════════════════════════════════════════════════
    # XAI / GROK
    # ═══════════════════════════════════════════════════════════════
    "xai_video_edit": {
        "lane": ["LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Edit video via xAI API",
    },
    "xai_video_extend": {
        "lane": ["LAB"],
        "min_tier": "B",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Extend video duration via xAI API",
    },

    # ═══════════════════════════════════════════════════════════════
    # YUANBAO / CHINESE SOCIAL
    # ═══════════════════════════════════════════════════════════════
    "yb_query_group_info": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Query Yuanbao group information",
    },
    "yb_query_group_members": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Query Yuanbao group members",
    },
    "yb_search_sticker": {
        "lane": ["DEV", "OPS", "LAB"],
        "min_tier": "C",
        "require_delegation": False,
        "eden_covenant": False,
        "description": "Search Yuanbao stickers",
    },
    "yb_send_dm": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Send a Yuanbao direct message",
    },
    "yb_send_sticker": {
        "lane": ["OPS"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Send a Yuanbao sticker",
    },

    # ═══════════════════════════════════════════════════════════════
    # COMPUTER USE
    # ═══════════════════════════════════════════════════════════════
    "computer_use": {
        "lane": ["DEV", "OPS"],
        "min_tier": "A",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Direct computer use (CUA) — screen + keyboard + mouse",
    },

    # ═══════════════════════════════════════════════════════════════
    # MCP GENERIC (catch-all for unlisted MCP tools)
    # ═══════════════════════════════════════════════════════════════
    "mcp_*": {
        "lane": ["DEV"],
        "min_tier": "B",
        "require_delegation": True,
        "eden_covenant": False,
        "description": "Generic MCP tool catch-all (unlisted MCP servers)",
    },
}


def get_tool_policy(tool_name: str) -> Dict[str, Any] | None:
    """Look up a tool's permission entry.

    Returns the policy dict or ``None`` if the tool is unlisted
    (allow-by-default).
    """
    entry = PERMISSION_MATRIX.get(tool_name)
    if entry is not None:
        return entry
    # Check wildcard catch-all
    if tool_name.startswith("mcp_") or tool_name.startswith("mcp/"):
        return PERMISSION_MATRIX.get("mcp_*")
    # No entry — unrestricted
    return None


def list_tools_for_lane(lane: str) -> list[str]:
    """Return all tool names that are permitted for *lane*."""
    lane_upper = lane.upper()
    result = []
    for name, entry in PERMISSION_MATRIX.items():
        allowed = entry.get("lane", [])
        if "ANY" in allowed or lane_upper in [l.upper() for l in allowed]:
            result.append(name)
    return sorted(result)


def list_tools_by_min_tier(tier: str) -> list[str]:
    """Return all tool names whose minimum tier is *tier* or stricter."""
    TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "ANY": -1}
    threshold = TIER_ORDER.get(tier.upper(), 4)
    result = []
    for name, entry in PERMISSION_MATRIX.items():
        min_tier = entry.get("min_tier", "D")
        min_val = TIER_ORDER.get(min_tier.upper(), 4)
        if min_val >= threshold:
            result.append(name)
    return sorted(result)
