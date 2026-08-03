#!/usr/bin/env python3
"""Eden OE — DB-Native Plugin Loader.

Reads plugin configurations from the ``core.eden`` database tables
(``plugins`` and ``plugin_hooks``) instead of scanning the filesystem
for ``plugin.yaml`` manifests.

Provides CRUD operations for plugin lifecycle management. If a
database query fails (table not found, DB not initialized, etc.)
the module falls back to filesystem-based plugin scanning via the
existing ``eden_cli.plugins`` infrastructure.

Usage::

    from eden.plugin_loader import (
        load_plugins,
        register_hooks,
        enable_plugin,
        disable_plugin,
        install_plugin,
        get_plugin,
    )

    # Load all enabled plugins
    plugins = load_plugins()

    # Register a specific plugin's hooks
    hooks = register_hooks("my_plugin")

    # Enable / disable lifecycle
    enable_plugin("my_plugin")
    disable_plugin("my_plugin")

    # Install a new plugin record
    install_plugin(
        "my_plugin",
        version="1.0.0",
        description="My custom plugin",
        hooks=[{"hook_name": "pre_tool_call", "handler": "my_mod.handler"}],
    )

Author: Eden (bootstrap assistant) — July 20, 2026
Refs: PLAYBOOK-EDEN-OE-COMPLETION Phase 2b
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from eden.db import EdenDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

PluginConfig = Dict[str, Any]
"""A single plugin record from the ``plugins`` table, as a plain dict.

Expected keys (auto-populated from DB row)::

    name        str   — unique plugin identifier
    version     str   — semver string
    description str   — human-readable summary
    source      str   — origin: ``bundled`` | ``user`` | ``project`` | ``pip``
    enabled     int   — 1 (enabled) or 0 (disabled)
    config      str   — JSON-encoded plugin-specific config (nullable)
"""

HookConfig = Dict[str, Any]
"""A single hook registration from the ``plugin_hooks`` table.

Expected keys::

    id          int    — auto-increment PK
    plugin_name str    — FK to ``plugins.name``
    hook_name   str    — hook identifier (e.g. ``pre_tool_call``)
    handler     str    — Python dotted path to the callback
    priority    int    — execution order (lower = earlier)
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> Dict[str, Any]:
    """Convert a ``sqlite3.Row`` to a plain ``dict``."""
    return dict(row) if row is not None else {}


def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
    """Convert a list of ``sqlite3.Row`` objects to plain dicts."""
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# DB-backed plugin CRUD
# ---------------------------------------------------------------------------


def load_plugins(db: Optional[EdenDB] = None) -> List[PluginConfig]:
    """Load all enabled plugins from ``core.eden → plugins``.

    Returns a list of plugin config dicts. Falls back to filesystem
    scanning if the database query fails.
    """
    db = db or EdenDB()
    try:
        rows = db.query("SELECT * FROM plugins WHERE enabled = 1")
        if rows is not None:
            return _rows_to_dicts(rows)
    except Exception as exc:
        logger.debug("DB plugin load failed, falling back to FS scan: %s", exc)

    return _fallback_fs_load()


def register_hooks(
    plugin_name: str,
    db: Optional[EdenDB] = None,
) -> List[HookConfig]:
    """Load hook registrations for *plugin_name* from ``plugin_hooks``.

    Returns hook config dicts sorted by priority. Falls back to
    scanning the filesystem if the DB query fails.
    """
    db = db or EdenDB()
    try:
        rows = db.query(
            "SELECT * FROM plugin_hooks "
            "WHERE plugin_name = ? "
            "ORDER BY priority ASC",
            (plugin_name,),
        )
        if rows is not None:
            return _rows_to_dicts(rows)
    except Exception as exc:
        logger.debug(
            "DB hook load failed for %s, falling back: %s", plugin_name, exc
        )

    return _fallback_fs_hooks(plugin_name)


def enable_plugin(name: str, db: Optional[EdenDB] = None) -> bool:
    """Enable a plugin by setting ``enabled = 1`` in the DB.

    Returns ``True`` if the update succeeded.
    """
    db = db or EdenDB()
    return db.execute("UPDATE plugins SET enabled = 1 WHERE name = ?", (name,))


def disable_plugin(name: str, db: Optional[EdenDB] = None) -> bool:
    """Disable a plugin by setting ``enabled = 0`` in the DB.

    Returns ``True`` if the update succeeded.
    """
    db = db or EdenDB()
    return db.execute("UPDATE plugins SET enabled = 0 WHERE name = ?", (name,))


def install_plugin(
    name: str,
    *,
    version: str = "0.1.0",
    description: str = "",
    source: str = "user",
    config: Optional[str] = None,
    hooks: Optional[List[Dict[str, Any]]] = None,
    db: Optional[EdenDB] = None,
) -> bool:
    """Insert a plugin record and its hooks into the database.

    Uses ``INSERT OR IGNORE`` so re-installation is safe (no-op on
    duplicate name). Returns ``True`` if the plugin row was inserted,
    ``False`` otherwise (e.g. already exists).

    *hooks* — optional list of hook dicts, each with ``hook_name``
    and ``handler`` keys, and optionally ``priority``.
    """
    db = db or EdenDB()

    inserted = db.execute(
        "INSERT OR IGNORE INTO plugins "
        "(name, version, description, source, enabled, config) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (name, version, description, source, config),
    )

    if hooks:
        for hook in hooks:
            db.execute(
                "INSERT OR IGNORE INTO plugin_hooks "
                "(plugin_name, hook_name, handler, priority) "
                "VALUES (?, ?, ?, ?)",
                (
                    name,
                    hook.get("hook_name", ""),
                    hook.get("handler", ""),
                    hook.get("priority", 0),
                ),
            )

    return inserted


def get_plugin(name: str, db: Optional[EdenDB] = None) -> Optional[PluginConfig]:
    """Get a single plugin by name from the database.

    Returns a plugin config dict or ``None`` if not found.
    Falls back to filesystem scan if the DB query fails.
    """
    db = db or EdenDB()
    try:
        rows = db.query("SELECT * FROM plugins WHERE name = ?", (name,))
        if rows:
            return _row_to_dict(rows[0])
    except Exception as exc:
        logger.debug("DB get_plugin failed for %s, falling back: %s", name, exc)

    return _fallback_fs_get(name)


def uninstall_plugin(name: str, db: Optional[EdenDB] = None) -> bool:
    """Remove a plugin and its hooks from the database.

    Returns ``True`` if any rows were deleted.
    """
    db = db or EdenDB()
    db.execute("DELETE FROM plugin_hooks WHERE plugin_name = ?", (name,))
    return db.execute("DELETE FROM plugins WHERE name = ?", (name,))


def list_all_plugins(db: Optional[EdenDB] = None) -> List[PluginConfig]:
    """Load **all** plugins (both enabled and disabled) from the DB.

    Falls back to filesystem scan if the query fails.
    """
    db = db or EdenDB()
    try:
        rows = db.query("SELECT * FROM plugins ORDER BY name")
        if rows is not None:
            return _rows_to_dicts(rows)
    except Exception as exc:
        logger.debug("DB list_all_plugins failed, falling back: %s", exc)

    return _fallback_fs_load()


# ---------------------------------------------------------------------------
# Filesystem fallback — calls into the existing eden_cli.plugins infra
# ---------------------------------------------------------------------------


def _fallback_fs_load() -> List[PluginConfig]:
    """Fallback: return plugin configs from ``PluginManager`` (FS-scanned)."""
    try:
        from eden_cli.plugins import get_plugin_manager

        pm = get_plugin_manager()
        pm.discover_and_load()
        return [
            {
                "name": p.manifest.name,
                "version": p.manifest.version,
                "description": p.manifest.description,
                "source": p.manifest.source or "filesystem",
                "enabled": 1 if p.enabled else 0,
                "config": None,
            }
            for p in pm._plugins.values()
        ]
    except Exception as exc:
        logger.debug("FS plugin load fallback failed: %s", exc)
        return []


def _fallback_fs_hooks(plugin_name: str) -> List[HookConfig]:
    """Fallback: extract hooks from a loaded filesystem plugin."""
    try:
        from eden_cli.plugins import get_plugin_manager

        pm = get_plugin_manager()
        pm.discover_and_load()
        plugin = pm._plugins.get(plugin_name)
        if plugin is not None:
            return [
                {
                    "plugin_name": plugin_name,
                    "hook_name": h,
                    "handler": h,
                    "priority": 0,
                }
                for h in plugin.hooks_registered
            ]
    except Exception as exc:
        logger.debug("FS hook fallback failed for %s: %s", plugin_name, exc)

    return []


def _fallback_fs_get(name: str) -> Optional[PluginConfig]:
    """Fallback: look up a single plugin from FS-scanned plugins."""
    try:
        from eden_cli.plugins import get_plugin_manager

        pm = get_plugin_manager()
        pm.discover_and_load()
        p = pm._plugins.get(name)
        if p is not None:
            return {
                "name": p.manifest.name,
                "version": p.manifest.version,
                "description": p.manifest.description,
                "source": p.manifest.source or "filesystem",
                "enabled": 1 if p.enabled else 0,
                "config": None,
            }
    except Exception as exc:
        logger.debug("FS get_plugin fallback failed for %s: %s", name, exc)

    return None
