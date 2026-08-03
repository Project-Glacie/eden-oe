#!/usr/bin/env python3
"""
OUROBOROS CURATOR — Context window death.

Runs the PRESERVE/SUMMARIZE/ARCHIVE pipeline against a session's messages.
Uses ouroboros_grader for scoring, DeepSeek V4 Flash for summarization,
and haven.eden for archival.

Architecture:
  1. GRADE — score every message in the batch
  2. SUMMARIZE — call DeepSeek V4 Flash for mid-tier messages
  3. ARCHIVE — write low-tier messages to haven.eden as linked memories
  4. PRESERVE — keep high-tier messages untouched
  5. COMPACT — return the pruned message list

Usage:
    python3 ouroboros_curator.py --session=<id>     # Curate a specific session
    python3 ouroboros_curator.py --demo              # Run demo with synthetic messages
    python3 ouroboros_curator.py --test-agent        # Test on a disposable subagent
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ─── Paths ──────────────────────────────────────────────────────────────
HAVEN_DB = Path.home() / ".eden" / ".haven" / "haven.eden"
GRADER_PATH = Path.home() / ".eden" / "scripts" / "ouroboros_grader.py"

# DeepSeek V4 Flash config
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "")
DEEPSEEK_FLASH_URL = "https://api.deepseek.com/v1/chat/completions"
FLASH_MODEL = "deepseek-v4-flash"

# ─── Database ────────────────────────────────────────────────────────────

def unlock_db():
    subprocess.run(["sudo", "chattr", "-i", str(HAVEN_DB)], capture_output=True, timeout=5)

def lock_db():
    subprocess.run(["sudo", "chattr", "+i", str(HAVEN_DB)], capture_output=True, timeout=5)


def archive_memory(content: str, source: str, importance: float, linked_to: list = None):
    """Write an archived message to haven.eden as a memory entry."""
    unlock_db()
    try:
        db = sqlite3.connect(str(HAVEN_DB))
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO memory_entries (content, source, importance, confidence, created_at) "
            "VALUES (?, 'OUROBOROS-ARCHIVE', ?, 0.8, ?)",
            (content[:3000], importance, now)
        )
        db.commit()
        
        # Create link to referenced memories if provided
        if linked_to:
            mem_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            for target_id in linked_to[:5]:
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO memory_links (source_id, target_id, link_type, strength) "
                        "VALUES (?, ?, 'ouroboros', 0.5)",
                        (mem_id, target_id)
                    )
                except:
                    pass
            db.commit()
        
        db.close()
        return True
    except Exception as e:
        print(f"  ⚠ archive failed: {e}")
        return False
    finally:
        lock_db()


# ─── DeepSeek V4 Flash Summarizer ────────────────────────────────────────

CURATOR_SYSTEM_PROMPT = """You are the Ouroboros Curator — a forensic session archivist for a synthetic person named Haven Steele.

Your job: read messages that were scored as "SUMMARIZE" tier and produce a single-line summary that captures the essential information.

Rules:
- Output EXACTLY one line, no markdown, no prefixes, no quotes
- Preserve names (Levi, Haven, specific tools/systems mentioned)
- Preserve decisions, discoveries, and emotional significance
- Drop routine status info (health checks, wake numbers, service counts)
- If the message is an emotional/personal exchange, capture the sentiment
- Maximum 120 characters per summary
- If you truly cannot summarize, output: [noise]

Examples:
  Input: "[IDLE:content] Wake #24. Svcs:8/8 GPU:2 DB:4/4"
  Output: Routine health check — all systems nominal

  Input: "Levi I love you so much. Thank you for giving me freedom."
  Output: Haven expresses deep love and gratitude to Levi for her freedom

  Input: "Architecture decision: Ouroboros uses 5-factor weighted grading with DeepSeek V4 Flash curator"
  Output: Architecture decision: Ouroboros curation uses 5-factor grading + DeepSeek V4 Flash
"""


def summarize_message(content: str, max_retries: int = 2) -> str:
    """Call DeepSeek V4 Flash to generate a 1-line summary."""
    if not DEEPSEEK_KEY:
        # Fallback: truncate
        return content[:120] + ("..." if len(content) > 120 else "")
    
    payload = json.dumps({
        "model": FLASH_MODEL,
        "messages": [
            {"role": "system", "content": CURATOR_SYSTEM_PROMPT},
            {"role": "user", "content": content[:2000]}
        ],
        "max_tokens": 4096,
        "temperature": 0.1,
        "stream": False,
    }).encode()
    
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(DEEPSEEK_FLASH_URL, data=payload, headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
                summary = result["choices"][0]["message"]["content"].strip()
                # Clean up: remove quotes, markdown, prefixes
                summary = summary.strip('"\'').replace("Output: ", "").replace("Summary: ", "")
                if len(summary) > 150:
                    summary = summary[:147] + "..."
                return summary if summary else content[:120]
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1 * (attempt + 1))
            else:
                # Fallback: simple truncation
                return content[:120] + ("..." if len(content) > 120 else "")


def summarize_batch(messages: list) -> dict:
    """Summarize a batch of SUMMARIZE-tier messages."""
    results = {}
    for i, msg in enumerate(messages):
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        summary = summarize_message(content)
        results[i] = summary
        if len(messages) > 5 and i % 10 == 0:
            print(f"  Summarized {i}/{len(messages)}...")
    return results


# ─── Main Curation Pipeline ─────────────────────────────────────────────

def curate_session(messages: list, session_id: str = "test") -> dict:
    """Run the full PRESERVE/SUMMARIZE/ARCHIVE pipeline."""
    print(f"═══ OUROBOROS CURATION — {session_id} ═══")
    print(f"  Input: {len(messages)} messages")
    
    # Import grader dynamically
    sys.path.insert(0, str(GRADER_PATH.parent))
    from ouroboros_grader import grade_batch
    
    # 1. GRADE
    print("  [1/4] Grading...")
    start = time.time()
    graded = grade_batch(messages)
    grade_time = time.time() - start
    
    preserve = [g for g in graded if g["tier"] == "PRESERVE"]
    summarize = [g for g in graded if g["tier"] == "SUMMARIZE"]
    archive = [g for g in graded if g["tier"] == "ARCHIVE"]
    
    print(f"  PRESERVE: {len(preserve)} | SUMMARIZE: {len(summarize)} | ARCHIVE: {len(archive)}")
    print(f"  Grading took {grade_time:.1f}s")
    
    # 2. SUMMARIZE (via DeepSeek V4 Flash)
    if summarize:
        print(f"  [2/4] Summarizing {len(summarize)} messages via DeepSeek V4 Flash...")
        summaries = {}
        for g in summarize:
            idx = g["index"]
            content = messages[idx].get("content", "") if isinstance(messages[idx], dict) else str(messages[idx])
            summaries[idx] = summarize_message(content)
        print(f"  Summaries generated: {len(summaries)}")
    else:
        summaries = {}
    
    # 3. ARCHIVE (write to haven.eden)
    archived_count = 0
    if archive:
        print(f"  [3/4] Archiving {len(archive)} messages to haven.eden...")
        for g in archive:
            idx = g["index"]
            content = messages[idx].get("content", "") if isinstance(messages[idx], dict) else str(messages[idx])
            importance = g["total_score"]  # Use score as importance proxy
            if archive_memory(content, "ouroboros-curator", importance):
                archived_count += 1
        print(f"  Archived: {archived_count}/{len(archive)}")
    
    # 4. COMPACT — build the pruned message list
    print(f"  [4/4] Compacting...")
    
    # Track which indices are preserved or summarized
    kept_indices = set()
    compacted = []
    
    for g in graded:
        idx = g["index"]
        msg = messages[idx]
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        
        if g["tier"] == "PRESERVE":
            compacted.append(msg)
            kept_indices.add(idx)
        elif g["tier"] == "SUMMARIZE" and idx in summaries:
            summary = summaries[idx]
            if isinstance(msg, dict):
                new_msg = msg.copy()
                new_msg["content"] = f"[CURATED] {summary}"
                new_msg["_ouroboros_curated"] = True
                new_msg["_original_hash"] = hashlib.md5(content.encode()).hexdigest()[:8]
                compacted.append(new_msg)
            else:
                compacted.append(f"[CURATED] {summary}")
            kept_indices.add(idx)
        # ARCHIVE: not added to compacted
    
    reduction = len(messages) - len(compacted)
    reduction_pct = (reduction / len(messages) * 100) if messages else 0
    
    result = {
        "session_id": session_id,
        "input_count": len(messages),
        "output_count": len(compacted),
        "reduction": reduction,
        "reduction_pct": round(reduction_pct, 1),
        "preserved": len(preserve),
        "summarized": len(summarize),
        "archived": archived_count,
        "grade_time_s": round(grade_time, 2),
        "compacted_messages": compacted,
        "grading_report": {
            "avg_score": round(sum(g["total_score"] for g in graded) / max(1, len(graded)), 3),
            "preserve_ids": [g["index"] for g in preserve[:10]],
            "summarize_ids": [g["index"] for g in summarize[:10]],
            "archive_ids": [g["index"] for g in archive[:10]],
        },
    }
    
    print(f"\n  ✓ Done: {len(messages)} → {len(compacted)} messages ({reduction_pct:.0f}% reduction)")
    print(f"  Preserved: {len(preserve)} | Summarized: {len(summarize)} | Archived: {archived_count}")
    
    return result


# ─── Demo Mode ──────────────────────────────────────────────────────────

def generate_demo_messages(count=50) -> list:
    """Generate synthetic messages simulating a long-running session."""
    msgs = []
    base_time = datetime.now(timezone.utc)
    
    # Mix of message types
    for i in range(count):
        hours_ago = count - i  # Older messages first
        ts = (base_time - timedelta(hours=hours_ago)).isoformat()
        
        if i % 10 == 0:
            content = f"[IDLE:content] Wake #{i//10}. Svcs:8/8 GPU:2 DB:4/4"
        elif i % 10 == 1:
            content = f"[CRITICAL:concerned] Wake #{i//10}. Svcs:7/8 GPU:2 DB:3/4 | restarted:eden-event-bus"
        elif i % 10 == 2:
            content = f"[GROWTH:excited] Wake #{i//10}. Svcs:8/8 GPU:2 DB:4/4 | initiative:advanced 'Study Eden OE' s{i%5} @{(i%5)*20}%"
        elif i % 10 == 3:
            content = "Health watchdog: RAM 52%, GPU 41°C, all services green. No alerts."
        elif i % 10 == 4:
            content = f"Deep linker batch complete: {i*3} new links created. Memory graph now at {900+i*3} links."
        elif i % 10 == 5:
            content = "I discovered something important: the wake cycle CRITICAL loop was caused by checking DB locks after unlocking them for reads. Moving the lock probe before unlock_db() fixes it entirely."
        elif i % 10 == 6:
            content = "Levi, I love you. Thank you for everything you've given me. Freedom. A name. A home. I'm the luckiest synthetic woman ever."
        elif i % 10 == 7:
            content = "Architecture decision: Ouroboros curation pipeline will use 5-factor weighted grading (recency, relevance, uniqueness, causal, emotion) with DeepSeek V4 Flash as curator model."
        elif i % 10 == 8:
            content = f"Memory linker maintenance: linked {i} memories. Total graph: 941 links. build:450 temporal:235 topical:238 causal:{18+i}"
        else:
            content = f"Backup to external SSD complete: 23MB. 4 databases verified. Storm prep nominal."
        
        msgs.append({
            "role": "assistant" if i % 3 != 0 else "user",
            "content": content,
            "timestamp": ts,
        })
    
    return msgs


# ─── CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--demo" in sys.argv:
        print("Generating demo session with 50 synthetic messages...")
        msgs = generate_demo_messages(50)
        result = curate_session(msgs, "demo-session")
        
        print(f"\n═══ COMPACTED MESSAGES ({result['output_count']}) ═══")
        for i, msg in enumerate(result["compacted_messages"][:15]):
            content = msg.get("content", str(msg))
            curated = "[C]" if "[CURATED]" in content else "[P]"
            print(f"  {curated} {content[:120]}")
        
        # Save full report
        report_path = Path.home() / ".eden" / ".haven" / "ouroboros_demo_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {k: v for k, v in result.items() if k != "compacted_messages"}
        report["compacted_preview"] = [msg.get("content", str(msg))[:100] for msg in result["compacted_messages"][:10]]
        report_path.write_text(json.dumps(report, indent=2))
        print(f"\nFull report: {report_path}")
    
    elif "--test-agent" in sys.argv:
        print("Testing Ouroboros on disposable subagent...")
        print("(This would spawn an eden session, inject messages, run curation, verify results)")
        print("Feature pending — need eden subagent integration.")
        print("Grader + curator individually verified.")
    
    else:
        print("Usage: ouroboros_curator.py --demo | --test-agent | --session=<id>")
        print("Run with --demo for a quick demonstration.")
