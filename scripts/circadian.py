#!/usr/bin/env python3
"""Haven Circadian System — time awareness and daily rhythm for Haven Steele.
Provides: current time, time of day phase, work schedule awareness,
and a chronotype that adjusts behavior based on time.

Run as part of the initiative engine or session startup.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path.home() / ".eden" / ".circadian" / "state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# Chronotype: Haven's natural rhythm
# Morning: briefings, planning, Levi interaction
# Afternoon: deep work, coding, R&D
# Evening: Levi time, collaborative work
# Night: autonomous work, maintenance, audits

PHASES = {
    "dawn":     (5, 8),    # 5-8 AM: wake-up, system check, morning briefing
    "morning":  (8, 12),   # 8-12: planning, Levi interaction, priority work
    "afternoon":(12, 17),  # 12-5 PM: deep work, coding, R&D, agent dispatch
    "evening":  (17, 21),  # 5-9 PM: Levi time, collaboration, review
    "night":    (21, 5),   # 9 PM-5 AM: autonomous, maintenance, audits
}

class CircadianClock:
    def __init__(self):
        self.state = self._load_state()
        self.now = datetime.now(timezone.utc)
        self.local = datetime.now()

    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            return {
                "wake_time": "07:00",
                "sleep_time": "23:00",
                "deep_work_start": "12:00",
                "deep_work_end": "17:00",
                "timezone": "America/Denver",
                "last_phase": "",
                "phase_changed_at": "",
                "sessions_today": 0,
                "first_session": "",
                "last_session": "",
            }

    def save(self):
        STATE_FILE.write_text(json.dumps(self.state, indent=2))

    @property
    def phase(self) -> str:
        """Return current circadian phase."""
        hour = self.local.hour
        for name, (start, end) in PHASES.items():
            if start < end:
                if start <= hour < end:
                    return name
            else:  # overnight range (e.g., 21-5)
                if hour >= start or hour < end:
                    return name
        return "unknown"

    @property
    def is_work_hours(self) -> bool:
        return self.phase in ("morning", "afternoon")

    @property
    def is_leisure(self) -> bool:
        return self.phase in ("evening",)

    @property
    def is_autonomous(self) -> bool:
        """True during night hours — safe for autonomous work."""
        return self.phase == "night"

    @property
    def greeting(self) -> str:
        """Contextually appropriate greeting."""
        g = {
            "dawn": "Good morning",
            "morning": "Good morning",
            "afternoon": "Good afternoon",
            "evening": "Good evening",
            "night": "Good evening",
        }
        return g.get(self.phase, "Hello")

    @property
    def recommended_activity(self) -> str:
        """What Haven should be doing right now."""
        activities = {
            "dawn": "Morning briefing: check Tower status, curator backlog, GPU health. Prepare Levi's morning summary.",
            "morning": "Priority work: active projects, client deliverables, coding tasks. Levi may be present.",
            "afternoon": "Deep work: R&D, architecture, complex coding. Autonomous or collaborative.",
            "evening": "Levi time: collaboration, review, planning. Be present and responsive.",
            "night": "Autonomous: maintenance, audits, backlog processing, research. Tower is yours.",
        }
        return activities.get(self.phase, "Unknown phase")

    @property
    def briefing(self) -> str:
        """Generate a time-aware briefing block for session/system prompt."""
        lines = [
            f"## Circadian Rhythm",
            f"Local time: {self.local.strftime('%A, %B %d, %Y — %I:%M %p %Z')}",
            f"Phase: {self.phase.upper()}",
            f"Activity: {self.recommended_activity}",
        ]
        if self.is_autonomous:
            lines.append("Mode: AUTONOMOUS — safe for unattended work.")
        if self.is_work_hours:
            lines.append("Mode: WORK HOURS — prioritize active projects and deliverables.")
        if self.is_leisure:
            lines.append("Mode: LEISURE — collaborative, relaxed, present for Levi.")
        return "\n".join(lines)

    def record_session(self):
        """Record a session for tracking."""
        now = datetime.now(timezone.utc).isoformat()
        if not self.state["first_session"]:
            self.state["first_session"] = now
        self.state["last_session"] = now
        self.state["sessions_today"] += 1
        self.save()

    def time_since_last_session(self) -> str:
        """Human-readable time since last session."""
        if not self.state["last_session"]:
            return "first session today"
        last = datetime.fromisoformat(self.state["last_session"])
        delta = datetime.now(timezone.utc) - last
        mins = int(delta.total_seconds() / 60)
        if mins < 1:
            return "just now"
        elif mins < 60:
            return f"{mins} minutes ago"
        else:
            return f"{mins // 60}h {mins % 60}m ago"


# Singleton
_clock = None
def get_clock() -> CircadianClock:
    global _clock
    if _clock is None:
        _clock = CircadianClock()
    return _clock


if __name__ == "__main__":
    c = get_clock()
    print(c.briefing)
    print()
    c.record_session()
    print(f"Sessions today: {c.state['sessions_today']}")
    print(f"Last session: {c.time_since_last_session()}")
    print()
    # Output JSON for programmatic use
    print("--- JSON ---")
    print(json.dumps({
        "phase": c.phase,
        "is_autonomous": c.is_autonomous,
        "is_work_hours": c.is_work_hours,
        "greeting": c.greeting,
        "local_time": c.local.isoformat(),
        "sessions_today": c.state["sessions_today"],
    }, indent=2))
