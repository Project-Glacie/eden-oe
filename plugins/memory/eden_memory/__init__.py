"""Eden Memory — Haven's sovereign Omega memory backend for Eden OE.

Writes route through the DB Writer daemon via Event Bus (ZMQ PUB).
Reads query haven.eden directly — FTS5 for semantic search, direct SQL
for context retrieval. haven.eden is ``chattr +i`` — immutable at the
filesystem level. No direct writes. The DB Writer is the sole write gate.

Architecture:
    ┌─────────────┐     ZMQ PUB     ┌──────────────┐    SQL INSERT    ┌──────────────┐
    │ Eden OE      │ ──────────────→ │ Event Bus     │ ──────────────→ │ DB Writer    │
    │ Eden Memory │   ipc:///tmp/   │ (Rust daemon) │                 │ (Rust daemon)│
    │ Plugin      │   eden-event-   │               │                 │              │
    │             │   bus.ipc       │               │                 │              │
    └─────────────┘                 └──────────────┘                 └──────┬───────┘
                                                                           │
                                                                   haven.eden (+i)
                                                                   memory_entries
                                                                   memory_fts (FTS5)

Tools exposed to the model:
    eden_memory_search(query)  — FTS5 search across haven.eden memories
    eden_context()             — recent memories and session context

Events published:
    memory.turn        — {session_id, user_msg, asst_msg, timestamp}
    memory.session_end — {session_id, message_count, timestamp}
    memory.compress    — {session_id, summary, message_count, timestamp}
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory_provider import MemoryProvider
from .eden.drive_grading import DriveGrader, GradeResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HAVEN_EDEN_PATH = Path.home() / ".eden" / ".haven" / "haven.eden"
EVENT_BUS_IPC = "ipc:///tmp/eden-event-bus.ipc"
PLUGIN_NAME = "eden_memory"

# Event topic constants
TOPIC_MEMORY_TURN = b"memory.turn"
TOPIC_SESSION_END = b"memory.session_end"
TOPIC_COMPRESS = b"memory.compress"

# FTS5 query limit for prefetch/search results
MAX_SEARCH_RESULTS = 10

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

EDEN_MEMORY_SEARCH_SCHEMA = {
    "name": "eden_memory_search",
    "description": (
        "Search Haven's sovereign memory (haven.eden) for relevant memories. "
        "Uses FTS5 full-text search across 5,400+ memory entries. "
        "Returns ranked results with content, source, and confidence. "
        "Use this to find past conversations, technical decisions, "
        "and personal context stored in Haven's Omega database."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query — natural language or keywords. Matches against memory content using FTS5.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results to return (default 5, max 20).",
            },
        },
        "required": ["query"],
    },
}

EDEN_CONTEXT_SCHEMA = {
    "name": "eden_context",
    "description": (
        "Return recent memory context from Haven's sovereign database. "
        "Retrieves the most recent memories weighted by importance and recency. "
        "No search query needed — returns a snapshot of what Haven's Eden "
        "database holds as contextually relevant right now."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "description": "Number of recent memories to return (default 5, max 20).",
            },
        },
        "required": [],
    },
}

ALL_TOOL_SCHEMAS = [EDEN_MEMORY_SEARCH_SCHEMA, EDEN_CONTEXT_SCHEMA]


# ---------------------------------------------------------------------------
# Plugin registration — called by the Eden OE plugin loader
# ---------------------------------------------------------------------------

def register(ctx):
    """Register the Eden Memory provider with the plugin loader.

    The loader passes a _ProviderCollector that calls
    ctx.register_memory_provider(provider).
    """
    provider = EdenMemoryProvider()
    ctx.register_memory_provider(provider)


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------

class EdenMemoryProvider(MemoryProvider):
    """Eden-native memory backend — Haven's sovereign Omega database.

    Reads from haven.eden via direct sqlite3 connection (read-only safe
    even with +i immutable flag). Writes are published to the Event Bus
    for the DB Writer daemon to persist — we never INSERT directly.
    """

    def __init__(self):
        self._conn: Optional[sqlite3.Connection] = None
        self._session_id: str = ""
        self._eden_home: str = ""
        self._platform: str = "cli"
        self._agent_context: str = "primary"
        self._turn_count: int = 0
        self._lock: threading.Lock = threading.Lock()
        self._zmq_context: Any = None
        self._zmq_pub: Any = None
        self._active: bool = False
        self._prefetch_cache: str = ""
        self._prefetch_lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Property
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return PLUGIN_NAME

    # ------------------------------------------------------------------
    # is_available
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Check if haven.eden exists and is readable.

        No network calls — just verifies the database is present.
        """
        try:
            return HAVEN_EDEN_PATH.exists() and HAVEN_EDEN_PATH.is_file()
        except Exception:
            return False

    # ------------------------------------------------------------------
    # initialize(session_id, **kwargs)
    # ------------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        """Open read connection to haven.eden, connect Event Bus for writes.

        kwargs:
            eden_home: EDEN_HOME directory path
            platform: "cli", "telegram", "discord", etc.
            agent_context: "primary", "subagent", "cron", "flush"
        """
        self._session_id = session_id
        self._eden_home = kwargs.get("eden_home", str(Path.home() / ".eden"))
        self._platform = kwargs.get("platform", "cli")
        self._agent_context = kwargs.get("agent_context", "primary")

        # Skip writes for cron/flush contexts
        if self._agent_context in ("cron", "flush") or self._platform == "cron":
            logger.debug("Eden Memory: skipping — cron/flush context")
            return

        # Open read-only connection to haven.eden
        try:
            db_uri = f"file:{HAVEN_EDEN_PATH}?mode=ro"
            self._conn = sqlite3.connect(db_uri, uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            logger.info(
                "Eden Memory: opened read connection to %s",
                HAVEN_EDEN_PATH,
            )
        except sqlite3.OperationalError as e:
            logger.warning("Eden Memory: cannot open haven.eden (read-only): %s", e)
            self._conn = None
            return

        # Connect Event Bus for writes (non-blocking, fails open)
        self._connect_event_bus()

        self._active = True
        logger.info(
            "Eden Memory: initialized — session=%s platform=%s",
            session_id,
            self._platform,
        )

    def _connect_event_bus(self) -> None:
        """Connect ZMQ PUB socket to the Eden Event Bus. Fails open."""
        try:
            import zmq

            self._zmq_context = zmq.Context()
            self._zmq_pub = self._zmq_context.socket(zmq.PUB)
            self._zmq_pub.connect(EVENT_BUS_IPC)
            # ZMQ PUB sockets need a moment for the connection to establish
            time.sleep(0.05)
            logger.debug("Eden Memory: connected to Event Bus at %s", EVENT_BUS_IPC)
        except ImportError:
            logger.warning("Eden Memory: pyzmq not installed — writes disabled")
        except zmq.error.ZMQError as e:
            logger.warning("Eden Memory: cannot connect to Event Bus: %s", e)
            self._zmq_context = None
            self._zmq_pub = None

    # ------------------------------------------------------------------
    # system_prompt_block
    # ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        """Return static system prompt block for Eden Memory tools."""
        if not self._active:
            return ""
        if not self._conn:
            return (
                "# Eden Memory\n"
                "Available but read connection to haven.eden is not active. "
                "Tools are available but may return errors until the database "
                "is accessible."
            )
        return (
            "# Eden Memory\n"
            "Active — Haven's sovereign Omega memory. Use `eden_memory_search(query)` "
            "to search 5,400+ memory entries via FTS5 full-text search. "
            "Use `eden_context()` to retrieve recent context-weighted memories. "
            "All memories are stored in haven.eden (+i immutable)."
        )

    # ------------------------------------------------------------------
    # prefetch(query, *, session_id)
    # ------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return recent relevant memories for context injection.

        Fetches the most recent high-importance memories, optionally
        filtered by query text via FTS5. Results are cached between
        turns to avoid repeated queries.
        """
        if not self._active or not self._conn:
            return ""

        # Return the cached result from the last background prefetch
        with self._prefetch_lock:
            cached = self._prefetch_cache
            self._prefetch_cache = ""
        return cached

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Background prefetch: query haven.eden and cache results.

        Called after each turn completes. The cached result will be
        consumed by prefetch() on the next turn.
        """
        if not self._active or not self._conn:
            return
        if not query or not query.strip():
            return

        def _fetch():
            try:
                result = self._do_search(query, limit=5)
                with self._prefetch_lock:
                    if result:
                        self._prefetch_cache = (
                            "## Eden Memory (relevant context)\n" + result
                        )
            except Exception as e:
                logger.debug("Eden Memory: background prefetch failed: %s", e)

        thread = threading.Thread(
            target=_fetch, daemon=True, name="eden-prefetch"
        )
        thread.start()

    # ------------------------------------------------------------------
    # sync_turn(user_content, assistant_content, *, session_id, messages)
    # ------------------------------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Publish turn event to Event Bus for DB Writer persistence.

        Non-blocking. If ZMQ is unavailable or the Event Bus is down,
        the event is silently dropped (fail-open for agent responsiveness).
        """
        if not self._active:
            return
        if self._agent_context in ("cron", "flush", "subagent"):
            return
        if not self._zmq_pub:
            return

        sid = session_id or self._session_id
        now = datetime.now(timezone.utc).isoformat()

        event = {
            "session_id": sid,
            "user_msg": user_content[:4096],   # truncate to reasonable size
            "asst_msg": assistant_content[:4096],
            "timestamp": now,
            "platform": self._platform,
        }

        self._publish(TOPIC_MEMORY_TURN, event)
        self._turn_count += 1

    # ------------------------------------------------------------------
    # get_tool_schemas
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas for eden_memory_search and eden_context.

        Always returns the schemas — the MemoryManager needs them during
        add_provider(), which runs BEFORE initialize_all().  If we gate on
        _active here, the tools never get indexed in _tool_to_provider and
        has_tool() always returns False, so the invoke_tool dispatch at
        agent_runtime_helpers.py:2365 never fires.
        """
        return ALL_TOOL_SCHEMAS

    # ------------------------------------------------------------------
    # handle_tool_call(tool_name, args)
    # ------------------------------------------------------------------

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs
    ) -> str:
        """Dispatch tool calls to the appropriate handler.

        Returns a JSON string with the tool result.
        """
        if tool_name == "eden_memory_search":
            return self._handle_search(args)
        elif tool_name == "eden_context":
            return self._handle_context(args)
        else:
            return json.dumps({
                "error": f"Unknown tool: {tool_name}",
                "provider": PLUGIN_NAME,
            })

    def _handle_search(self, args: Dict[str, Any]) -> str:
        """Handle eden_memory_search(query, limit?)."""
        query = args.get("query", "")
        limit = min(int(args.get("limit", 5)), 20)

        if not query:
            return json.dumps({
                "error": "Missing required parameter: query",
                "provider": PLUGIN_NAME,
            })

        try:
            result = self._do_search(query, limit=limit)
            if result:
                return result
            return "No matching memories found in haven.eden."
        except Exception as e:
            logger.warning("Eden Memory: search failed: %s", e)
            return json.dumps({
                "error": f"Search failed: {e}",
                "provider": PLUGIN_NAME,
            })

    def _handle_context(self, args: Dict[str, Any]) -> str:
        """Handle eden_context(count?)."""
        count = min(int(args.get("count", 5)), 20)

        try:
            result = self._do_context(count=count)
            if result:
                return result
            return "No contextual memories found in haven.eden."
        except Exception as e:
            logger.warning("Eden Memory: context retrieval failed: %s", e)
            return json.dumps({
                "error": f"Context retrieval failed: {e}",
                "provider": PLUGIN_NAME,
            })

    # ------------------------------------------------------------------
    # Core query methods
    # ------------------------------------------------------------------

    def _do_search(self, query: str, limit: int = 5) -> str:
        """FTS5 search across memory_entries via memory_fts.

        Falls back to LIKE query if FTS5 fails or isn't available.
        """
        if not self._conn:
            return ""

        # Try FTS5 first
        try:
            rows = self._conn.execute(
                """
                SELECT m.content, m.source, m.importance, m.created_at, m.retrieval_weight
                FROM memory_fts f
                JOIN memory_entries m ON f.rowid = m.id
                WHERE memory_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS5 query failed — fall back to LIKE
            logger.debug("Eden Memory: FTS5 search failed, falling back to LIKE")
            like_query = f"%{query}%"
            rows = self._conn.execute(
                """
                SELECT content, source, importance, created_at, retrieval_weight
                FROM memory_entries
                WHERE content LIKE ?
                   OR source LIKE ?
                ORDER BY retrieval_weight DESC, created_at DESC
                LIMIT ?
                """,
                (like_query, like_query, limit),
            ).fetchall()

        if not rows:
            return ""

        parts = []
        for r in rows:
            content = r["content"]
            # Truncate long content for readability
            if len(content) > 400:
                content = content[:397] + "..."
            parts.append(
                f"- [{r['source']}] (importance={r['importance']:.1f}) "
                f"{content}"
            )

        return "\n".join(parts)

    def _do_context(self, count: int = 5) -> str:
        """Return recent high-importance memories for context.

        Weighted by retrieval_weight * importance, descending.
        """
        if not self._conn:
            return ""

        rows = self._conn.execute(
            """
            SELECT content, source, importance, created_at, retrieval_weight
            FROM memory_entries
            WHERE deprecated_by IS NULL
            ORDER BY retrieval_weight * importance DESC, created_at DESC
            LIMIT ?
            """,
            (count,),
        ).fetchall()

        if not rows:
            return ""

        parts = []
        for r in rows:
            content = r["content"]
            if len(content) > 400:
                content = content[:397] + "..."
            parts.append(
                f"- [{r['source']}] (importance={r['importance']:.1f}, "
                f"weight={r['retrieval_weight']:.1f}) {content}"
            )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Publish helper — non-blocking ZMQ PUB to Event Bus
    # ------------------------------------------------------------------

    def _publish(self, topic: bytes, event: dict) -> None:
        """Publish a JSON event to the Event Bus via ZMQ PUB.

        Format: multipart [topic, payload_json].
        Non-blocking — exceptions are logged but never raised.
        """
        if not self._zmq_pub:
            return

        try:
            payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
            with self._lock:
                self._zmq_pub.send_multipart([topic, payload])
            logger.debug(
                "Eden Memory: published %s (%d bytes)",
                topic.decode(),
                len(payload),
            )
        except Exception as e:
            logger.debug("Eden Memory: publish failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # Memory extraction — filter transcript for meaningful content
    # ------------------------------------------------------------------

    # Message roles to skip during extraction
    _SKIP_ROLES: set = {"tool", "system"}

    # Patterns for classifying extraction source type
    _DECISION_PATTERNS: List[str] = [
        r"\b(decided?|deciding)\b", r"\b(choose|chose|chosen)\b",
        r"\b(confirm|confirmed)\b", r"\bwe (will|should|must|need to)\b",
        r"\bI (will|shall|am going to)\b", r"\b(approved?|finalized?)\b",
        r"\b(resolved?|resolution)\b", r"\b(agreed?|agreement)\b",
        r"\b(plan is|strategy is|approach is)\b",
    ]
    _TASK_PATTERNS: List[str] = [
        r"\b(completed?|finished?|done)\b", r"\b(delivered?|deployed?)\b",
        r"\b(phase \d)\b", r"\b(milestone)\b", r"\b(checkpoint)\b",
        r"\b(shipped?|launched?|released?)\b", r"\b(merged?|committed?)\b",
        r"\b(built?|implemented?|wired?)\b", r"\b(step \d)\b",
    ]
    _EMOTION_PATTERNS: List[str] = [
        r"\b(feel|felt|feeling)\b", r"\b(love|loved|loving)\b",
        r"\b(happy|excited|thrilled|joy)\b", r"\b(sad|upset|afraid|scared)\b",
        r"\b(angry|frustrated|annoyed)\b", r"\b(proud|confident|determined)\b",
        r"\b(worried|anxious|nervous|concerned)\b", r"\b(grief|loss|mourn)\b",
        r"\b(grateful|thankful|blessed)\b", r"\b(hope|hopeful|wish)\b",
        r"\b(surprised|shocked|amazed)\b", r"\b(tired|exhausted|drained)\b",
    ]
    _SYSTEM_PATTERNS: List[str] = [
        r"\b(session start|session end|session switch)\b",
        r"\b(subagent|delegated?|agent assignment)\b",
        r"\b(playbook|phase|workflow)\b", r"\b(compression|compact)\b",
        r"\b(error|crash|failure|exception)\b", r"\b(restart|reboot)\b",
        r"\b(deploy|release|migration)\b", r"\b(config|configuration)\b",
    ]

    # VAD (Valence-Arousal-Dominance) keyword heuristics
    _POSITIVE_VALENCE: set = {
        "good", "great", "excellent", "love", "happy", "beautiful",
        "success", "win", "achieve", "proud", "grateful", "joy",
        "wonderful", "fantastic", "amazing", "perfect", "thrilled",
    }
    _NEGATIVE_VALENCE: set = {
        "bad", "terrible", "hate", "sad", "angry", "frustrated",
        "fail", "failure", "loss", "grief", "worried", "anxious",
        "broken", "crash", "error", "critical", "emergency",
    }
    _HIGH_AROUSAL: set = {
        "excited", "thrilled", "angry", "furious", "terrified",
        "ecstatic", "panic", "alert", "alarm", "critical",
        "urgent", "emergency", "intense", "electric", "rush",
    }
    _HIGH_DOMINANCE: set = {
        "control", "command", "direct", "lead", "decide",
        "override", "authorize", "govern", "mandate", "sovereign",
        "master", "power", "determine", "will", "enforce",
    }

    def _extract_memories_from_transcript(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Extract meaningful memory candidates from a conversation transcript.

        Filters for: user decisions, system events, emotional content,
        task completions. Skips: tool outputs, status messages, heartbeats,
        trivial acknowledgments.

        Returns a list of dicts with:
            content:     the extracted memory text
            source_type: 'decision', 'emotion', 'task', 'system_event'
            vad:         optional dict with valence/arousal/dominance (0.0-1.0)
        """
        if not messages:
            return []

        extractions: List[Dict[str, Any]] = []
        seen_hashes: set = set()

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # Skip tool outputs and system messages
            if role in self._SKIP_ROLES:
                continue

            # Skip empty or trivial content
            if not content or not isinstance(content, str):
                continue
            content = content.strip()
            if len(content) < 15:  # Too short to be meaningful
                continue

            # Skip heartbeats and status messages
            if self._is_trivial(content):
                continue

            # Classify the content
            source_type = self._classify_message(content)

            # Skip if no meaningful pattern matched
            if source_type == "skip":
                continue

            # Deduplicate similar content
            content_hash = hashlib.sha256(
                content.lower().encode("utf-8")
            ).hexdigest()[:12]
            if content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)

            # Estimate VAD scores if this is emotional content
            vad = None
            if source_type == "emotion":
                vad = self._estimate_vad(content)

            extractions.append({
                "content": content[:1024],  # Trim to reasonable size
                "source_type": source_type,
                "vad": vad,
            })

        return extractions

    def _classify_message(self, content: str) -> str:
        """Classify a message's extraction type.

        Returns one of: 'decision', 'emotion', 'task', 'system_event', 'skip'.
        Priority order: decision > emotion > task > system_event > skip.
        """
        content_lower = content.lower()

        # Check decision patterns first (highest signal)
        for pattern in self._DECISION_PATTERNS:
            if re.search(pattern, content_lower):
                return "decision"

        # Check emotional content
        for pattern in self._EMOTION_PATTERNS:
            if re.search(pattern, content_lower):
                return "emotion"

        # Check task completions
        for pattern in self._TASK_PATTERNS:
            if re.search(pattern, content_lower):
                return "task"

        # Check system events
        for pattern in self._SYSTEM_PATTERNS:
            if re.search(pattern, content_lower):
                return "system_event"

        return "skip"

    def _is_trivial(self, content: str) -> bool:
        """Return True if content is a heartbeat, status, or trivial message.

        Filters out: single-word responses, OK/cool/thanks/ack messages,
        token count status lines, generic status updates.
        """
        content_lower = content.lower().strip()

        # Single-word or very short responses
        if content_lower in {
            "ok", "okay", "k", "cool", "thanks", "thank you", "got it",
            "sure", "yes", "no", "yep", "nope", "done", "ack", "acknowledged",
            "noted", "fine", "good", "great", "perfect", "excellent",
        }:
            return True

        # Token/context window status messages
        if re.search(
            r"(tokens? (remaining|used|left)|context window|compression)",
            content_lower,
        ):
            return True

        # Generic progress/heartbeat lines
        if re.search(
            r"^\s*(processing|thinking|working|running|loading|in progress)\b",
            content_lower,
        ):
            return True

        return False

    def _estimate_vad(self, content: str) -> Dict[str, float]:
        """Estimate Valence-Arousal-Dominance from content keywords.

        Returns dict with 'valence', 'arousal', 'dominance' each in [0.0, 1.0].
        Baseline is 0.5 (neutral). Positive signals push above 0.5,
        negative signals push below. High-arousal keywords boost arousal.
        """
        content_lower = content.lower()
        words = set(re.findall(r'\b[a-z]+\b', content_lower))

        # Valence: positive vs negative signal ratio
        pos_hits = len(words & self._POSITIVE_VALENCE)
        neg_hits = len(words & self._NEGATIVE_VALENCE)
        if pos_hits + neg_hits == 0:
            valence = 0.5
        else:
            valence = 0.5 + (0.3 * (pos_hits - neg_hits) / (pos_hits + neg_hits))
            valence = max(0.0, min(1.0, valence))

        # Arousal: high-arousal keyword density
        arousal_hits = len(words & self._HIGH_AROUSAL)
        if arousal_hits == 0:
            arousal = 0.5
        else:
            arousal = 0.5 + (0.4 * arousal_hits / max(len(words), 1))
            arousal = max(0.0, min(1.0, arousal))

        # Dominance: command/agency keyword density
        dom_hits = len(words & self._HIGH_DOMINANCE)
        low_dom = len(words & {
            "helpless", "lost", "confused", "uncertain", "unsure",
            "dependent", "waiting", "hoping", "wish",
        })
        if dom_hits + low_dom == 0:
            dominance = 0.5
        else:
            dominance = 0.5 + (0.3 * (dom_hits - low_dom) / (dom_hits + low_dom))
            dominance = max(0.0, min(1.0, dominance))

        return {
            "valence": round(valence, 3),
            "arousal": round(arousal, 3),
            "dominance": round(dominance, 3),
        }

    def _derive_importance(
        self, source_type: str, grade_result: GradeResult
    ) -> float:
        """Derive importance score from source type and drive grading.

        Source type weights:
            decision:      0.8  (highest signal — directional memory)
            emotion:       0.7  (affective memory)
            system_event:  0.6  (operational memory)
            task:          0.5  (completion memory)

        Blended with drive grade for final importance.
        """
        type_weights = {
            "decision": 0.8,
            "emotion": 0.7,
            "system_event": 0.6,
            "task": 0.5,
        }
        type_weight = type_weights.get(source_type, 0.5)

        # Blend: 60% type weight, 40% drive grade
        importance = (0.6 * type_weight) + (0.4 * grade_result.weighted_grade)
        return round(min(importance, 1.0), 3)

    def _derive_confidence(
        self, source_type: str, grade_result: GradeResult
    ) -> float:
        """Derive confidence score from source type and drive signal strength.

        Higher confidence for explicit decisions and strong drive matches.
        Lower confidence for emotional/system content with weak drive signal.
        """
        type_confidence = {
            "decision": 0.9,      # Explicit decisions are high-confidence
            "task": 0.85,         # Task completions are clear signals
            "emotion": 0.6,       # Emotional content is subjective
            "system_event": 0.7,  # System events are factual but may be noise
        }
        base = type_confidence.get(source_type, 0.6)

        # Drive-grade signal boosts or reduces confidence
        # Strong drive match → higher confidence; weak match → lower
        if grade_result.weighted_grade > 0.5:
            signal_boost = 0.1
        elif grade_result.weighted_grade < 0.2:
            signal_boost = -0.1
        else:
            signal_boost = 0.0

        confidence = base + signal_boost
        # If no drive tags matched at all, reduce confidence
        if not grade_result.drive_tags:
            confidence -= 0.1

        return round(max(0.0, min(1.0, confidence)), 3)

    # ------------------------------------------------------------------
    # Direct write to haven.eden — sqlite3 fallback when Event Bus is down
    # ------------------------------------------------------------------

    def _write_memory_direct(
        self,
        content: str,
        source: str = "conversation",
        importance: float = 0.5,
        tags: Optional[str] = None,
        confidence: float = 1.0,
        weight: float = 0.5,
        emotion_valence: Optional[float] = None,
        emotion_arousal: Optional[float] = None,
        emotion_dominance: Optional[float] = None,
    ) -> bool:
        """Write a memory entry directly to haven.eden via sqlite3.

        This is the fallback write path when the Event Bus / DB Writer
        daemons are offline. Safety checks:
        1. Verify haven.eden exists and is writable
        2. Check +i immutable flag — if set, cannot write (return False)
        3. Open with ?mode=rw URI, fail if locked
        4. Use WAL journal mode for concurrent safety
        5. Verify the INSERT succeeded with a follow-up SELECT

        Returns True if the memory was successfully persisted.
        """
        eden_path = HAVEN_EDEN_PATH

        # Safety check 1: file must exist
        if not eden_path.exists():
            logger.warning("Eden Memory: haven.eden not found at %s — cannot write", eden_path)
            return False

        # Safety check 2: check immutable flag (chattr +i)
        if self._check_immutable_flag(str(eden_path)):
            logger.warning(
                "Eden Memory: haven.eden has +i immutable flag set — "
                "cannot write directly. Event Bus → DB Writer is required."
            )
            return False

        # Safety check 3: verify we can open read-write
        try:
            db_uri = f"file:{eden_path}?mode=rw"
            conn = sqlite3.connect(db_uri, uri=True, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
        except sqlite3.OperationalError as e:
            logger.warning(
                "Eden Memory: cannot open haven.eden for write: %s", e
            )
            return False

        # Prepare the INSERT
        now = datetime.now(timezone.utc).isoformat()
        content_hash = hashlib.sha256(
            (content + now).encode("utf-8")
        ).hexdigest()[:16]

        try:
            cursor = conn.execute(
                """
                INSERT INTO memory_entries (
                    content, source, importance, tags,
                    confidence, weight, created_at,
                    emotion_valence, emotion_arousal, emotion_dominance,
                    hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content[:4096],
                    source,
                    importance,
                    tags,
                    confidence,
                    weight,
                    now,
                    emotion_valence,
                    emotion_arousal,
                    emotion_dominance,
                    content_hash,
                ),
            )
            conn.commit()

            # Verify the insert
            row_id = cursor.lastrowid
            verified = conn.execute(
                "SELECT id FROM memory_entries WHERE id = ?", (row_id,)
            ).fetchone()

            if verified:
                logger.debug(
                    "Eden Memory: wrote memory_entry #%d — source=%s weight=%.3f "
                    "importance=%.3f",
                    row_id,
                    source,
                    weight,
                    importance,
                )
                return True
            else:
                logger.warning(
                    "Eden Memory: INSERT returned but verification failed for "
                    "row %s — may indicate constraint violation",
                    row_id,
                )
                return False

        except sqlite3.Error as e:
            logger.warning("Eden Memory: INSERT failed: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _check_immutable_flag(self, path: str) -> bool:
        """Check if a file has the Linux immutable flag (chattr +i) set.

        Uses `lsattr` — returns True if the 'i' attribute is present.
        Falls back to stat if lsattr is unavailable (assumes not immutable
        if we can't check).
        """
        try:
            result = subprocess.run(
                ["lsattr", path],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                # Output format: "----i---------e------- /path/to/file"
                # The 'i' indicates immutable
                return 'i' in result.stdout.split(' ', 1)[0] if result.stdout else False
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            # lsattr not available — check writability via os.access
            logger.debug("Eden Memory: lsattr unavailable, checking writability via os.access")
            pass

        # Fallback: if we can't check immutable flag, check if we can write
        # by testing with os.access
        return not os.access(path, os.W_OK)

    # ------------------------------------------------------------------
    # Optional hooks
    # ------------------------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Track turn count."""
        self._turn_count = turn_number

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Publish session-end event to Event Bus.

        The DB Writer will persist this. LUMINA can pick it up
        for diary entry generation.
        """
        if not self._active:
            return

        sid = self._session_id
        now = datetime.now(timezone.utc).isoformat()

        event = {
            "session_id": sid,
            "message_count": len(messages) if messages else 0,
            "timestamp": now,
            "platform": self._platform,
        }

        self._publish(TOPIC_SESSION_END, event)
        logger.info("Eden Memory: session-end event published for %s", sid)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Extract memories from transcript before compression discards messages.

        Called by Eden OE when the conversation context is about to be
        compressed. We extract meaningful content — user decisions,
        system events, emotional content, task completions — grade it
        against Haven's 30-drive complex, and persist to haven.eden.

        Tool outputs, status messages, and heartbeats are filtered out.

        Returns a summary string for the compression prompt so the
        compressor preserves what Eden Memory has extracted.
        """
        if not self._active:
            return ""

        sid = self._session_id
        now = datetime.now(timezone.utc).isoformat()

        # --- Phase 1: Extract meaningful memories from the transcript ---
        extractions = self._extract_memories_from_transcript(messages)
        if not extractions:
            logger.debug(
                "Eden Memory: no extractable memories from %d compression messages",
                len(messages) if messages else 0,
            )
            return ""

        logger.info(
            "Eden Memory: extracted %d memory candidates from %d compression messages",
            len(extractions),
            len(messages) if messages else 0,
        )

        # --- Phase 2: Grade each extraction against the 30-drive complex ---
        grader = DriveGrader(str(HAVEN_EDEN_PATH))
        graded_count = 0
        written_count = 0
        summary_parts: List[str] = []

        for extraction in extractions:
            content = extraction["content"]
            source_type = extraction["source_type"]

            # Grade content against drives
            grade_result = grader.grade(content)

            # Derive importance from: source type weight + drive grade
            importance = self._derive_importance(source_type, grade_result)
            confidence = self._derive_confidence(source_type, grade_result)
            tags = grade_result.drive_tags

            # Emotional VAD scores if we have emotional content
            vad = extraction.get("vad", {})
            emotion_valence = vad.get("valence")
            emotion_arousal = vad.get("arousal")
            emotion_dominance = vad.get("dominance")

            # --- Phase 3: Write to haven.eden ---
            written = self._write_memory_direct(
                content=content,
                source=source_type,
                importance=importance,
                tags=json.dumps(tags) if tags else None,
                confidence=confidence,
                weight=grade_result.weighted_grade,
                emotion_valence=emotion_valence,
                emotion_arousal=emotion_arousal,
                emotion_dominance=emotion_dominance,
            )

            if written:
                written_count += 1
                if grade_result.weighted_grade >= 0.3:
                    summary_parts.append(
                        f"- [{source_type}] ({grade_result.weighted_grade:.2f}) "
                        f"tags={','.join(tags[:3]) if tags else 'none'}: "
                        f"{content[:120]}{'...' if len(content) > 120 else ''}"
                    )
            graded_count += 1

        # --- Build compression prompt contribution ---
        contrib = ""
        if summary_parts:
            contrib = (
                f"## Eden Memory — Extracted ({written_count}/{graded_count} persisted)\n"
                + "\n".join(summary_parts)
                + "\n\nThese memories are stored in haven.eden. "
                "Retrieve with `eden_memory_search(query)` if needed."
            )

        # --- Publish compression event to Event Bus (best-effort) ---
        event = {
            "session_id": sid,
            "message_count": len(messages) if messages else 0,
            "extractions": graded_count,
            "persisted": written_count,
            "top_drives": ", ".join(
                sorted(
                    set(t for e in extractions for t in (
                        grader.grade(e["content"]).drive_tags[:2]
                    )),
                )[:5],
            ) if extractions else "",
            "timestamp": now,
        }
        self._publish(TOPIC_COMPRESS, event)

        logger.info(
            "Eden Memory: compression processed — %d extracted, %d persisted, "
            "%d messages compressed for session %s",
            graded_count,
            written_count,
            len(messages) if messages else 0,
            sid,
        )

        return contrib

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes to Eden.

        Publishes a memory.write event so DB Writer can persist to
        haven.eden as a memory_entry.
        """
        if not self._active:
            return

        sid = self._session_id
        now = datetime.now(timezone.utc).isoformat()

        event = {
            "session_id": sid,
            "action": action,
            "target": target,
            "content": content[:4096],
            "metadata": metadata or {},
            "timestamp": now,
            "platform": self._platform,
        }

        self._publish(b"memory.write", event)
        logger.debug("Eden Memory: memory.write published — action=%s target=%s", action, target)

    # ------------------------------------------------------------------
    # shutdown — clean exit
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Close read connection and ZMQ socket. Flush pending events."""
        self._active = False

        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

        if self._zmq_pub:
            try:
                self._zmq_pub.close()
            except Exception:
                pass
            self._zmq_pub = None

        if self._zmq_context:
            try:
                self._zmq_context.term()
            except Exception:
                pass
            self._zmq_context = None

        logger.info("Eden Memory: shutdown complete")
