#!/usr/bin/env python3
"""access_gate.py — PC-Control + Capability Gate (pre_tool_call hook) v2

CLASSIFIED / NEED-TO-KNOW / DADT — Ranger only.
Per Levi's orders (2026-08-01 classification; 2026-08-02 role-based access):

  - Terminal/file/PC-change tools: restricted to authorized operators.
  - Discord write-capable tools (discord_admin, discord_message_post,
    ranger_data_write): allowed only for Levi (invariant, full access) or
    users with explicit member/role grants recorded in the permissions
    registry (access_control.eden) — grants Levi teaches, nothing more.
  - Read/note tools: not gated here (availability is toolset-controlled).

Identity: EDEN_OE_SESSION_USER_ID is exported per-subprocess by the hook
runner (agent/shell_hooks.py, Levi directive 2026-08-02). Absent identity
(cron / self-heal / local autonomous) => log-only, the behavioral gate
applies at the agent level. Gated tools with an unknown NON-empty user id
are BLOCKED fail-closed.

Roles: resolved from the member_roles cache; refreshed from Discord REST
(GET /guilds/<guild>/members/<user>) with a TTL. Cache lives in
access_control.eden so role membership is auditable.

Contract (agent/shell_hooks.py pre_tool_call):
  stdin  -> {"tool_name": "...", "tool_input": {...}, "session_id": "...", "extra": {...}}
  stdout -> {"action": "block", "message": "..."}  (or silent no-op {})
"""

import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ACCESS_DB = Path(os.environ.get(
    "EDEN_ACCESS_DB",
    str(Path.home() / ".eden" / "data" / "access_control.eden"),
))
CONF = os.path.expanduser("~/.config/systemd/user/eden-gateway.service.d/discord.conf")
GUILD = "1521002232286285946"  # Echo Detachment guild (matches discord_admin template)

LEVI = "232374677287337996"  # S-tier custodian — full access, always
ROLE_CACHE_TTL_SECONDS = 900  # 15 min

# Tools that change state — anything here needs Levi or an explicit grant.
GATED_TOOLS = {
    "terminal", "write_file", "patch", "execute_code", "delete_file",
    "process", "cronjob", "systemctl", "docker", "docker_exec",
    "skill_manage", "memory", "delegate_task",
    "browser_click", "browser_type", "browser_navigate",
    "mcp_filesystem_write_file", "mcp_filesystem_delete_file",
    "mcp_filesystem_move_file",
    # Discord write-capable tools (enabled 2026-08-02 per Levi)
    "discord_admin", "discord_message_post", "ranger_data_write",
}

# Read/note tools are NOT gated here — toolset availability controls them.


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(ACCESS_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, session_id TEXT, user_id TEXT, tool TEXT,
            decision TEXT, reason TEXT, env_has_user INTEGER
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS permissions (
            tool TEXT NOT NULL,             -- tool name or '*' (all tools)
            principal_type TEXT NOT NULL,   -- 'user' | 'role'
            principal_id TEXT NOT NULL,     -- discord user id | role id
            granted_by TEXT NOT NULL,       -- who authorized (Levi)
            granted_at TEXT NOT NULL,
            PRIMARY KEY (tool, principal_type, principal_id)
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS member_roles (
            user_id TEXT PRIMARY KEY,
            role_ids TEXT NOT NULL,         -- JSON array
            refreshed_at TEXT NOT NULL
        )""")
    return con


def log_decision(con, session_id, user_id, tool, decision, reason, env_has_user):
    try:
        con.execute(
            "INSERT INTO access_log (ts, session_id, user_id, tool, decision, reason, env_has_user) "
            "VALUES (?,?,?,?,?,?,?)",
            (now_iso(), session_id, user_id, tool, decision, reason, 1 if env_has_user else 0))
        con.commit()
    except Exception:
        pass


def _bot_token() -> str:
    env = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if env:
        return env
    with open(CONF) as f:
        m = re.search(r'DISCORD_BOT_TOKEN=("?)([A-Za-z0-9._-]+)', f.read())
    if not m:
        raise RuntimeError("no discord token")
    return m.group(2)


def _discord_get(path: str) -> dict | list:
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path,
        headers={"Authorization": "Bot " + _bot_token(),
                 "User-Agent": "DiscordBot (ranger-access-gate, 1.0)"})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
        return json.loads(body) if body else {}


def _cached_roles(con, user_id: str) -> list:
    row = con.execute(
        "SELECT role_ids, refreshed_at FROM member_roles WHERE user_id=?",
        (user_id,)).fetchone()
    if row:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[1])).total_seconds()
            if age < ROLE_CACHE_TTL_SECONDS:
                return json.loads(row[0])
        except Exception:
            pass
    # Refresh from Discord REST
    try:
        member = _discord_get(f"/guilds/{GUILD}/members/{user_id}")
        roles = member.get("roles", []) if isinstance(member, dict) else []
        con.execute(
            "INSERT OR REPLACE INTO member_roles (user_id, role_ids, refreshed_at) VALUES (?,?,?)",
            (user_id, json.dumps(roles), now_iso()))
        con.commit()
        return roles
    except Exception:
        # Fail-closed: on refresh failure return [] (caller blocks non-Levi)
        return []


def _is_granted(con, user_id: str, tool: str) -> bool:
    """Explicit user or role grant for the tool (or a wildcard '*')."""
    grants = {r[0] for r in con.execute(
        "SELECT DISTINCT tool FROM permissions WHERE tool IN (?, '*') "
        "AND principal_type='user' AND principal_id=?",
        (tool, user_id))}
    if grants:
        return True
    for role in _cached_roles(con, user_id):
        if con.execute(
                "SELECT 1 FROM permissions WHERE tool IN (?, '*') "
                "AND principal_type='role' AND principal_id=? LIMIT 1",
                (tool, role)).fetchone():
            return True
    return False


def main() -> int:
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or payload.get("args") or {}
    session_id = str(payload.get("session_id") or payload.get("extra", {}).get("session_id") or "")

    # Non-gated tools: no gate needed (log-only path not even hit).
    if tool not in GATED_TOOLS:
        return 0

    user_id = os.environ.get("EDEN_OE_SESSION_USER_ID", "").strip()
    con = get_db()

    if not user_id:
        # No session identity (cron / self-heal / local CLI / autonomous).
        # Ranger's own autonomous operations — log and allow; the behavioral
        # gate ("who is this for?") applies at the agent level.
        log_decision(con, session_id, user_id, tool, "log_only",
                     "no session user env — autonomous/cron/local path", False)
        print(json.dumps({}))
        return 0

    if user_id == LEVI:
        log_decision(con, session_id, user_id, tool, "allow",
                     "S-tier custodian — full access (invariant)", True)
        print(json.dumps({}))
        return 0

    try:
        if _is_granted(con, user_id, tool):
            log_decision(con, session_id, user_id, tool, "allow",
                         "explicit grant (user or role)", True)
            print(json.dumps({}))
            return 0
    except Exception as e:
        # Fail-closed: resolution error blocks the call.
        log_decision(con, session_id, user_id, tool, "block",
                     f"grant resolution error: {e}", True)
        print(json.dumps({
            "action": "block",
            "message": "⚠️ Access check failed (fail-closed). This action requires authorization. Verify with the custodian."}))
        return 0

    log_decision(con, session_id, user_id, tool, "block",
                 f"user {user_id} has no grant for {tool}", True)
    print(json.dumps({
        "action": "block",
        "message": "⚠️ This action requires a role/member grant from the custodian (Levi). No grant is recorded for this tool."}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
