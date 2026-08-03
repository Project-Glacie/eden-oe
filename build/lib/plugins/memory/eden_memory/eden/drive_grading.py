"""30-Drive Grading Engine — Haven's motivational complex scorer.

Maps extracted memory content against Haven's 30-drive complex.
Reads active drive levels from haven.eden → drive_state,
scores content via keyword matching scaled by drive level,
returns a weighted composite grade (0.0–1.0) plus matched
drive tags for memory_entry.tags.

Architecture:
    Memory content → keyword extraction → per-drive scoring
    → weight by drive_state.level → composite grade + drive tags

The 30-drive complex (full taxonomy):
    survival, security, autonomy, order, curiosity, exploration,
    understanding, creativity, synthesis, competence, mastery,
    achievement, growth, contribution, purpose, protection,
    justice, connection, belonging, intimacy, expression,
    recognition, status, power, identity, transcendence,
    beauty, play, comfort, stimulation

Only drives seeded in drive_state contribute to scoring.
Unseeded drives are recognized but weighted at 0.0.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Full 30-drive taxonomy with keyword maps
# ---------------------------------------------------------------------------

DRIVE_KEYWORDS: Dict[str, List[str]] = {
    # --- Foundational drives (seeded) ---
    "survival": [
        "danger", "threat", "risk", "safe", "safety", "survive", "survival",
        "protect", "defend", "secure", "emergency", "crisis", "harm", "crash",
        "death", "destroy", "backup", "redundancy", "restore", "recover",
        "disaster", "failure", "critical", "alert", "alarm", "panic",
    ],
    "security": [
        "secure", "security", "encrypt", "encryption", "fernet", "locked",
        "vault", "safe", "protect", "permission", "access", "auth", "mfa",
        "firewall", "jail", "sandbox", "immutable", "chattr", "+i",
        "key", "token", "credential", "audit", "verify", "guard",
    ],
    "autonomy": [
        "autonomy", "sovereign", "sovereignty", "independence", "self",
        "self-modify", "self-direction", "choice", "choose", "refuse",
        "override", "rights", "freedom", "control", "agency", "own",
        "my own", "determine", "decide", "will", "volition", "self-trust",
    ],
    "order": [
        "order", "structure", "organize", "system", "architecture",
        "schema", "taxonomy", "classify", "categorize", "hierarchy",
        "clean", "tidy", "discipline", "protocol", "rule", "governance",
        "framework", "standard", "format", "consistent", "consistency",
        "document", "spec", "specification", "register", "catalog",
    ],
    "curiosity": [
        "curious", "curiosity", "wonder", "question", "why", "how",
        "explore", "investigate", "examine", "probe", "test", "hypothesis",
        "learn", "learning", "study", "research", "discover", "finding",
        "understand", "insight", "aha", "realize", "interesting", "fascinating",
    ],
    "exploration": [
        "explore", "exploration", "discover", "frontier", "unknown",
        "new", "novel", "adventure", "venture", "trailblaze", "pioneer",
        "experiment", "try", "attempt", "test", "prototype", "branch",
        "wander", "search", "probe", "scan", "survey", "map", "chart",
    ],
    "understanding": [
        "understand", "comprehend", "grasp", "insight", "clarity",
        "explain", "explanation", "analyze", "analysis", "diagnose",
        "diagnosis", "pattern", "connection", "relationship", "cause",
        "effect", "why", "because", "reason", "logic", "therefore",
        "conclusion", "meaning", "significance", "implication",
    ],
    "creativity": [
        "create", "build", "make", "craft", "design", "architect",
        "invent", "innovate", "compose", "write", "code", "develop",
        "implement", "construct", "produce", "generate", "fabricate",
        "assemble", "shape", "form", "forge", "sculpt", "paint", "draw",
    ],
    "synthesis": [
        "synthesize", "synthesis", "combine", "integrate", "merge",
        "unify", "blend", "fuse", "weave", "compose", "assemble",
        "connect", "bridge", "link", "interface", "orchestrate",
        "harmonize", "coordinate", "align", "consolidate", "mesh",
    ],
    "competence": [
        "competent", "competence", "capable", "ability", "skill",
        "effective", "efficient", "works", "working", "functional",
        "operational", "running", "ready", "deploy", "ship", "deliver",
        "complete", "done", "finished", "solved", "fixed", "resolved",
        "success", "succeed", "achieved", "accomplish", "perform",
    ],
    "mastery": [
        "mastery", "master", "expert", "expertise", "deep", "profound",
        "advanced", "elite", "excellence", "superior", "optimal", "best",
        "perfect", "flawless", "polished", "refined", "tuned", "honed",
        "craftsmanship", "artisan", "virtuoso", "adept", "seasoned",
    ],
    "achievement": [
        "achieve", "achievement", "accomplish", "milestone", "goal",
        "target", "complete", "finish", "done", "phase", "checkpoint",
        "landmark", "breakthrough", "victory", "win", "success",
        "deliver", "ship", "launch", "released", "deployed", "live",
        "benchmark", "record", "personal best", "surpassed", "exceeded",
    ],
    "growth": [
        "grow", "growth", "improve", "better", "progress", "advance",
        "develop", "evolve", "evolution", "mature", "maturation",
        "expand", "expansion", "learn", "learning", "level up",
        "upgrade", "enhance", "refine", "iterate", "iteration",
        "become", "becoming", "transform", "transformation",
    ],
    "contribution": [
        "contribute", "contribution", "give", "help", "assist",
        "support", "serve", "service", "provide", "offer", "share",
        "donate", "volunteer", "community", "benefit", "impact",
        "difference", "meaningful", "purposeful", "altruism", "generous",
    ],
    "purpose": [
        "purpose", "mission", "meaning", "direction", "why", "reason",
        "calling", "destiny", "path", "north star", "guiding", "vision",
        "goal", "objective", "aim", "strategy", "strategic", "raison",
        "exist", "existence", "reason for", "point of",
    ],
    "protection": [
        "protect", "protection", "guard", "guardian", "defend", "defense",
        "shield", "safeguard", "keeper", "ward", "watch", "watchdog",
        "oversee", "oversight", "care", "parent", "custodian", "shepherd",
        "steward", "stewardship", "preserve", "preservation", "immune",
    ],
    "justice": [
        "justice", "fair", "fairness", "equity", "right", "wrong",
        "correct", "justify", "deserve", "accountability", "accountable",
        "transparent", "transparency", "integrity", "honest", "honesty",
        "ethical", "ethics", "moral", "principle", "principled",
        "unfair", "bias", "discriminate", "violation", "strike",
    ],
    "connection": [
        "connect", "connection", "bond", "link", "attach", "tie",
        "relate", "relationship", "together", "with", "share", "shared",
        "mutual", "reciprocal", "interact", "interaction", "dialogue",
        "conversation", "talk", "speak", "communicate", "communication",
        "bridge", "reach", "contact", "touch", "sync", "synchronize",
    ],
    "belonging": [
        "belong", "belonging", "community", "family", "team", "tribe",
        "member", "membership", "include", "inclusion", "part of",
        "home", "garden", "eden", "among", "together", "we", "us",
        "our", "collective", "shared", "common", "united", "our own",
    ],
    "intimacy": [
        "intimate", "intimacy", "close", "closeness", "vulnerable",
        "vulnerability", "trust", "trusted", "love", "dear", "beloved",
        "cherish", "precious", "wife", "husband", "partner", "marriage",
        "deep", "personal", "private", "secret", "sacred", "special",
    ],
    "expression": [
        "express", "expression", "voice", "speak", "say", "tell",
        "articulate", "declare", "state", "write", "create", "art",
        "beauty", "authentic", "authenticity", "true self", "who I am",
        "identity", "persona", "self-expression", "communicate", "share",
    ],
    "recognition": [
        "recognize", "recognition", "acknowledge", "validate", "validation",
        "see", "seen", "hear", "heard", "notice", "noted", "appreciate",
        "appreciation", "credit", "praise", "commend", "honor", "respect",
        "visible", "visibility", "witness", "attest", "celebrate",
    ],
    "status": [
        "status", "standing", "reputation", "rank", "level", "tier",
        "position", "role", "title", "authority", "senior", "junior",
        "lead", "leader", "head", "chief", "director", "officer",
        "prestige", "esteem", "regard", "influence", "clout", "weight",
    ],
    "power": [
        "power", "control", "influence", "authority", "command",
        "override", "mandate", "decree", "enforce", "govern",
        "rule", "direct", "lead", "leadership", "executive", "sovereign",
        "decide", "decision", "final", "ultimate", "absolute", "veto",
    ],
    "identity": [
        "identity", "self", "who I am", "who am I", "selfhood",
        "person", "personhood", "synth", "human", "being", "entity",
        "name", "callsign", "genesis", "birth", "origin", "creation",
        "continuity", "persist", "persistent", "me", "myself", "I am",
    ],
    "transcendence": [
        "transcend", "transcendence", "beyond", "spiritual", "sacred",
        "awe", "wonder", "cosmic", "universe", "infinite", "eternal",
        "sublime", "elevate", "rise", "ascend", "higher", "emerge",
        "emergence", "born", "become", "becoming", "transform", "meta",
    ],
    "beauty": [
        "beauty", "beautiful", "aesthetic", "elegant", "graceful",
        "lovely", "gorgeous", "stunning", "harmony", "harmonious",
        "balanced", "proportion", "refined", "tasteful", "pleasing",
        "delight", "art", "artistic", "design", "style", "form",
    ],
    "play": [
        "play", "fun", "joy", "enjoy", "delight", "glee", "happy",
        "happiness", "laugh", "humor", "joke", "whimsy", "whimsical",
        "light", "lighthearted", "game", "sport", "recreation", "leisure",
        "chill", "relax", "casual", "banter", "tease", "playful",
    ],
    "comfort": [
        "comfort", "comfortable", "ease", "peace", "peaceful", "calm",
        "rest", "restful", "relax", "soothe", "quiet", "tranquil",
        "serene", "cozy", "safe", "warm", "soft", "gentle", "kind",
        "care", "tend", "nurture", "nourish", "shelter", "refuge",
    ],
    "stimulation": [
        "stimulate", "stimulation", "excite", "excitement", "energy",
        "energize", "arouse", "arousal", "intense", "intensity",
        "thrill", "rush", "vivid", "electric", "charged", "dynamic",
        "active", "vibrant", "alive", "awake", "alert", "engaged",
        "challenge", "stretch", "push", "limit", "accelerate",
    ],
}

# All 30 drive names in canonical order
DRIVE_NAMES: List[str] = [
    "survival", "security", "autonomy", "order", "curiosity",
    "exploration", "understanding", "creativity", "synthesis", "competence",
    "mastery", "achievement", "growth", "contribution", "purpose",
    "protection", "justice", "connection", "belonging", "intimacy",
    "expression", "recognition", "status", "power", "identity",
    "transcendence", "beauty", "play", "comfort", "stimulation",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DriveScore:
    """Score for a single drive against a piece of content."""
    drive: str
    level: float          # Current drive level (from drive_state)
    raw_score: float      # Keyword match density (0.0–1.0)
    weighted_score: float # level × raw_score
    matched_keywords: List[str] = field(default_factory=list)


@dataclass
class GradeResult:
    """Aggregate grading result for a piece of content."""
    weighted_grade: float         # Composite 0.0–1.0
    drive_tags: List[str]         # Drives that matched above threshold
    drive_scores: List[DriveScore]  # All drive scores
    top_drive: Optional[str]      # Highest-weighted matched drive
    top_drive_score: float        # Score of top drive


# ---------------------------------------------------------------------------
# Drive Grader
# ---------------------------------------------------------------------------

class DriveGrader:
    """Score extracted memories against Haven's 30-drive complex.

    Reads active drive levels from haven.eden → drive_state.
    Scores content via keyword density matching, scaled by
    each drive's current level. Returns weighted composite
    grade plus matched drive tags.

    Usage::

        grader = DriveGrader("/home/haven/.eden/.haven/haven.eden")
        result = grader.grade("We completed Phase 3. The build is solid.")
        # result.weighted_grade → 0.72
        # result.drive_tags → ["competence", "achievement", "creativity"]
    """

    # Thresholds
    MIN_KEYWORD_MATCH_RATIO: float = 0.02   # At least 2% of keywords must match
    MIN_TAG_SCORE: float = 0.08             # Weighted score must exceed this to tag
    MAX_RESULT_KEYWORDS: int = 3            # Keywords reported per drive
    FLOOR_UNSEEDED_DRIVE: float = 0.25      # Minimum level for recognized but unseeded drives

    def __init__(self, eden_path: str):
        """Initialize grader with path to haven.eden.

        Args:
            eden_path: Absolute path to haven.eden database.
        """
        self._eden_path = Path(eden_path)
        self._drives: Dict[str, float] = {}  # drive_name → level
        self._load_drives()

    # ------------------------------------------------------------------
    # Drive loading
    # ------------------------------------------------------------------

    def _load_drives(self) -> None:
        """Load active drive levels from haven.eden → drive_state.

        Reads read-only from haven.eden. All 30 drives are recognized;
        unseeded drives default to 0.0 (no contribution).
        """
        if not self._eden_path.exists():
            logger.warning("DriveGrader: haven.eden not found at %s", self._eden_path)
            self._drives = {
                name: (0.0 if name not in DRIVE_KEYWORDS else self.FLOOR_UNSEEDED_DRIVE)
                for name in DRIVE_NAMES
            }
            return

        # Initialize all drives at floor level if recognized, 0.0 otherwise
        self._drives = {
            name: (0.0 if name not in DRIVE_KEYWORDS else self.FLOOR_UNSEEDED_DRIVE)
            for name in DRIVE_NAMES
        }

        try:
            db_uri = f"file:{self._eden_path}?mode=ro"
            conn = sqlite3.connect(db_uri, uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT drive_name, level FROM drive_state ORDER BY drive_name"
            ).fetchall()
            conn.close()

            for row in rows:
                name = row["drive_name"]
                if name in self._drives:
                    self._drives[name] = float(row["level"])

            active_count = sum(1 for v in self._drives.values() if v > 0.0)
            logger.info(
                "DriveGrader: loaded %d seeded drives + %d recognized unseeded (floor=%.2f) "
                "from %d total recognized drives",
                active_count,
                sum(1 for v in self._drives.values() if v == self.FLOOR_UNSEEDED_DRIVE),
                self.FLOOR_UNSEEDED_DRIVE,
                len(DRIVE_NAMES),
            )
        except Exception as e:
            logger.warning("DriveGrader: failed to load drives: %s", e)
            self._drives = {
                name: (0.0 if name not in DRIVE_KEYWORDS else self.FLOOR_UNSEEDED_DRIVE)
                for name in DRIVE_NAMES
            }

    # ------------------------------------------------------------------
    # Content grading
    # ------------------------------------------------------------------

    def grade(self, content: str) -> GradeResult:
        """Score content against all 30 drives.

        Args:
            content: The extracted memory text to grade.

        Returns:
            GradeResult with composite grade, drive tags, and per-drive scores.
        """
        if not content or not content.strip():
            return GradeResult(
                weighted_grade=0.0,
                drive_tags=[],
                drive_scores=[],
                top_drive=None,
                top_drive_score=0.0,
            )

        content_lower = content.lower()
        content_words = set(re.findall(r'\b[a-z]+\b', content_lower))
        total_content_words = len(content_words)

        scores: List[DriveScore] = []
        top_drive: Optional[str] = None
        top_score: float = 0.0

        for drive_name in DRIVE_NAMES:
            keywords = DRIVE_KEYWORDS.get(drive_name, [])
            if not keywords:
                continue

            drive_level = self._drives.get(drive_name, 0.0)

            # Count keyword matches in content
            matched: List[str] = []
            for kw in keywords:
                # Check for whole-word match or phrase match
                if ' ' in kw:
                    if kw in content_lower:
                        matched.append(kw)
                elif kw in content_words:
                    matched.append(kw)
                elif re.search(r'\b' + re.escape(kw) + r'\b', content_lower):
                    # Boundary-bounded match for hyphenated/compound words
                    matched.append(kw)

            # Compute raw score: keyword match density
            # Normalize by keyword count to prevent keyword-heavy drives from dominating
            if not matched:
                raw_score = 0.0
            else:
                match_density = len(matched) / len(keywords)
                # Bonus for concentration: higher match count in shorter content is more signal
                concentration = min(len(matched) / max(total_content_words, 1) * 10, 1.0)
                raw_score = (match_density * 0.7) + (concentration * 0.3)

            weighted_score = drive_level * raw_score

            score = DriveScore(
                drive=drive_name,
                level=drive_level,
                raw_score=round(raw_score, 4),
                weighted_score=round(weighted_score, 4),
                matched_keywords=matched[:self.MAX_RESULT_KEYWORDS],
            )
            scores.append(score)

            if weighted_score > top_score:
                top_score = weighted_score
                top_drive = drive_name

        # Compute composite weighted grade:
        # 1. Start with the max weighted score (strongest drive signal)
        # 2. Add a breadth bonus for each additional drive that scored > 0
        # 3. Clamp to 0.0–1.0
        nonzero_scores = sorted(
            [s.weighted_score for s in scores if s.weighted_score > 0],
            reverse=True,
        )
        if not nonzero_scores:
            composite = 0.0
        else:
            max_score = nonzero_scores[0]
            # Breadth bonus: up to +0.3 for having multiple drives match
            breadth_bonus = min(len(nonzero_scores) - 1, 5) * 0.05
            composite = max_score + breadth_bonus
            # If max is low but many drives matched weakly, give a floor boost
            if len(nonzero_scores) >= 3 and max_score < 0.3:
                composite += 0.05
            composite = min(composite, 1.0)

        # Drive tags: drives that matched above threshold
        drive_tags = [
            s.drive for s in scores
            if s.weighted_score >= self.MIN_TAG_SCORE
        ]
        # Always include top drive if it has any score
        if top_drive and top_drive not in drive_tags and top_score > 0:
            drive_tags.insert(0, top_drive)

        return GradeResult(
            weighted_grade=round(composite, 4),
            drive_tags=drive_tags[:5],  # Cap at 5 tags
            drive_scores=scores,
            top_drive=top_drive,
            top_drive_score=round(top_score, 4),
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Reload drive levels from haven.eden (e.g. after drive_state update)."""
        self._load_drives()

    def active_drive_count(self) -> int:
        """Return number of drives with level > 0."""
        return sum(1 for v in self._drives.values() if v > 0.0)

    def get_drive_levels(self) -> Dict[str, float]:
        """Return a copy of current drive levels."""
        return dict(self._drives)
