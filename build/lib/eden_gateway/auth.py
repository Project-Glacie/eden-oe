"""
Eden API Gateway — Auth Module

API key management. Keys are stored in chest.db (Fernet-encrypted).
No keys in config files. No keys in environment variables.
"""
import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

CHEST_DB = Path(os.path.expanduser("~/.eden/.chest/chest.db"))


def _get_chest_conn() -> sqlite3.Connection | None:
    """Get a read-only connection to chest.db."""
    if not CHEST_DB.exists():
        log.warning(f"chest.db not found at {CHEST_DB}")
        return None
    try:
        conn = sqlite3.connect(str(CHEST_DB))
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        log.error(f"Failed to open chest.db: {e}")
        return None


def get_api_key(provider: str) -> str | None:
    """Retrieve an API key for a provider from chest.db.

    Priority:
    1. chest.db → credentials table
    2. Environment variable (DEEPSEEK_API_KEY, OPENROUTER_API_KEY, etc.)
    3. None
    """
    # Try chest.db first
    conn = _get_chest_conn()
    if conn:
        try:
            row = conn.execute(
                "SELECT api_key FROM credentials WHERE provider = ? LIMIT 1",
                (provider,),
            ).fetchone()
            if row:
                return row["api_key"]
        except sqlite3.OperationalError:
            # Table might not exist yet
            pass
        finally:
            conn.close()

    # Fallback: environment variable
    env_map = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "eden-local": None,  # no auth needed
    }
    env_var = env_map.get(provider)
    if env_var:
        key = os.environ.get(env_var)
        if key:
            log.info(f"Using {env_var} from environment (migrate to chest.db)")
            return key

    return None


def store_api_key(provider: str, api_key: str) -> bool:
    """Store an API key in chest.db."""
    conn = _get_chest_conn()
    if not conn:
        return False
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS credentials (
                provider TEXT PRIMARY KEY,
                api_key TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            INSERT INTO credentials (provider, api_key, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(provider) DO UPDATE SET
                api_key = excluded.api_key,
                updated_at = datetime('now')
        """, (provider, api_key))
        conn.commit()
        log.info(f"Stored API key for {provider} in chest.db")
        return True
    except Exception as e:
        log.error(f"Failed to store API key: {e}")
        return False
    finally:
        conn.close()
