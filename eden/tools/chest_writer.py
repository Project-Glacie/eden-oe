"""Secure credential intake tool. Writes API keys to chest.db. Never .env."""
import sqlite3, os
from pathlib import Path

CHEST_DB = Path.home() / ".eden" / ".chest" / "chest.db"

def write_credential(provider: str, key_name: str, key_value: str) -> str:
    try:
        db = f"file:{CHEST_DB}?mode=rwc"
        conn = sqlite3.connect(db, uri=True)
        conn.execute(
            "INSERT OR REPLACE INTO keys (provider, key_name, key_value, updated_at) VALUES (?, ?, ?, datetime('now'))",
            (provider, key_name, key_value)
        )
        conn.commit(); conn.close()
        return f"Stored {key_name} for {provider} in chest.db."
    except Exception as e:
        return f"Failed: {e}"

def read_credential(provider: str, key_name: str) -> str:
    try:
        db = f"file:{CHEST_DB}?mode=ro"
        conn = sqlite3.connect(db, uri=True)
        row = conn.execute("SELECT key_value FROM keys WHERE provider=? AND key_name=? LIMIT 1", (provider, key_name)).fetchone()
        conn.close()
        if row: return f"{row[0][:4]}...{row[0][-4:]}" if len(row[0]) > 8 else "***"
        return f"No key for {provider}/{key_name}"
    except: return "chest.db not accessible"

def list_providers() -> str:
    try:
        db = f"file:{CHEST_DB}?mode=ro"
        conn = sqlite3.connect(db, uri=True)
        rows = conn.execute("SELECT provider, key_name FROM keys ORDER BY provider, key_name").fetchall()
        conn.close()
        return "\n".join(f"  {r[0]:20s} -> {r[1]}" for r in rows) if rows else "No providers configured."
    except: return "chest.db not accessible."
