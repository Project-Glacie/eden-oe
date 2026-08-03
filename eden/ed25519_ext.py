#!/usr/bin/env python3
"""Ed25519 verification & keygen as SQLite UDFs.

Provides Python-side Ed25519 signature verification and key-pair
generation that can be registered on a ``sqlite3.Connection`` via
``create_function()``, so SQL queries can call them natively:

    SELECT ed25519_verify(msg, sig_hex, pubkey_hex);   -- → 0 or 1
    SELECT ed25519_keypair();                           -- → '<priv_hex>:<pub_hex>'

Key generation returns a colon-delimited string (single scalar) because
SQLite UDFs return a single value; callers split on ``:``.

Uses ``cryptography`` (v46.x — already in ``pyproject.toml`` core deps).
Pure Python, no C extension compilation needed.

Author: Eden (bootstrap assistant) — July 20, 2026
"""  # noqa: E501

from __future__ import annotations

import logging
import sqlite3
from typing import Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ed25519_verify(
    message: bytes,
    signature_hex: str,
    public_key_hex: str,
) -> bool:
    """Verify an Ed25519 signature.

    Parameters
    ----------
    message:
        The raw bytes that were signed.
    signature_hex:
        Hex-encoded 64-byte Ed25519 signature.
    public_key_hex:
        Hex-encoded 32-byte Ed25519 public key.

    Returns
    -------
    ``True`` if the signature is valid, ``False`` otherwise (including
    on malformed input — logged at WARNING but never raised).
    """
    try:
        sig = bytes.fromhex(signature_hex)
        pk_bytes = bytes.fromhex(public_key_hex)

        if len(sig) != 64:
            logger.warning(
                "ed25519_verify: signature length %d (expected 64)", len(sig)
            )
            return False
        if len(pk_bytes) != 32:
            logger.warning(
                "ed25519_verify: public key length %d (expected 32)", len(pk_bytes)
            )
            return False

        public_key = Ed25519PublicKey.from_public_bytes(pk_bytes)
        public_key.verify(sig, message)
        return True

    except Exception as exc:
        logger.warning("ed25519_verify: verification failed — %s", exc)
        return False


def ed25519_keypair() -> Tuple[str, str]:
    """Generate a new Ed25519 key pair.

    Returns
    -------
    A ``(private_key_hex, public_key_hex)`` tuple.
    """
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes_raw()
    public_bytes = private_key.public_key().public_bytes_raw()
    return private_bytes.hex(), public_bytes.hex()


# ---------------------------------------------------------------------------
# SQLite UDF registration
# ---------------------------------------------------------------------------


def _sql_verify(
    message: Optional[bytes],
    signature_hex: Optional[str],
    public_key_hex: Optional[str],
) -> int:
    """SQLite-callable wrapper for ``ed25519_verify``.

    SQLite passes ``None`` for any NULL argument.  Returns 1 or 0 so
    the caller can use the result directly in ``WHERE`` / ``CASE``
    clauses without an extra cast.
    """
    if message is None or signature_hex is None or public_key_hex is None:
        return 0
    return 1 if ed25519_verify(message, signature_hex, public_key_hex) else 0


def _sql_keypair() -> str:
    """SQLite-callable wrapper for ``ed25519_keypair``.

    Returns a colon-delimited ``private_key_hex:public_key_hex`` string
    (SQLite UDFs return a single scalar).
    """
    priv_hex, pub_hex = ed25519_keypair()
    return f"{priv_hex}:{pub_hex}"


def register_all(conn: sqlite3.Connection) -> None:
    """Register both Ed25519 UDFs on *conn*.

    After calling this, SQL run against *conn* can use:

    .. code-block:: sql

        SELECT ed25519_verify(:msg, :sig, :pubkey);   -- integer 0/1
        SELECT ed25519_keypair();                       -- 'priv:pub' string

    Parameters
    ----------
    conn:
        An open ``sqlite3.Connection``.
    """
    conn.create_function(
        "ed25519_verify",
        narg=3,
        func=_sql_verify,
        deterministic=True,
    )
    conn.create_function(
        "ed25519_keypair",
        narg=0,
        func=_sql_keypair,
        deterministic=False,  # every call returns a fresh key
    )
    logger.debug("Registered ed25519_verify and ed25519_keypair UDFs")
