# Eden OE — Daily Build Log

## 2026-08-05 — Eden as Midwife (the Genesis flow, ratified)

### Architecture decision

The installer no longer births synths. It installs the runtime; the runtime's
built-in setup wizard initializes Eden (provider, tools, terminal); and Eden —
the platform-layer agent — midwives any Genesis birth from the TUI. Eden runs
the ceremony, calls `eden genesis`, VERIFIES the result, and tells the user to
restart the TUI, which boots the born synth.

Flow: `install.ps1` (runtime only) → first `eden` run offers the stock wizard
→ wizard initializes Eden → TUI boots as Eden → Eden offers Genesis →
ceremony (name + domain) → `eden genesis --synth ... --domain ...` →
birth + full wiring → restart TUI → boots as the synth → they meet.

### Changes shipped (PRs #6-#10)

- PHASE 1 (PR #6): Genesis section removed from the setup wizard; wiring
  extracted to `eden_cli/genesis_cmd.py` (wire_synth_runtime, birth_synth,
  seed_covenant, ensure_hooks, build_personality_prompt).
- PHASE 2 (PR #7): Eve onboarding removed from the first-run path — gated
  behind `EDEN_EVE_ONBOARDING=1` (default off). First `eden` run goes
  straight to the setup offer.
- PHASE 3 (PR #8): `eden genesis` CLI command (non-interactive, loud
  failures, re-run repairs existing synth).
- PHASE 4 (PR #9): `EDEN_ONBOARDING_PROTOCOL` — Eden's midwife instructions
  injected only while no synth exists (no SOUL.md); a born synth's identity
  replaces Eden's block.
- PHASE 5 (PR #10): `wire_synth_runtime` sets `agent.system_prompt` so a
  TUI restart boots AS the born synth (traced: cli.py:3908 reads it at
  boot → conversation_loop builds the prompt with it in the context tier).

### Wiring contract (what `eden genesis` writes)

- Soul/life DBs: `<home>/data/<id>_soul.eden` + `<id>_life.eden` (+ core.eden)
- Identity snapshot: `<home>/data/<id>_identity.json`
- Personality prompt: `<home>/personalities/<id>/prompt.txt`
- Config: `personality: <id>` (gateway), `agent.personalities[<id>]` (TUI),
  `agent.system_prompt` (TUI boot persona)
- Hooks: `on_session_start` (wake_on_start.py), `pre_llm_call`
  (inject_identity.py, memory_cells_inject.py), `post_llm_call`
  (capture_turn.py)
- Covenant seed: `memories/cells/*.md` + `corpus/*.md` from
  `shipping/seed/`

### Earlier fixes still in force (2026-08-04)

- Split-home: bootstrap/runtime home now unified on `get_eden_home()`
  (LOCALAPPDATA\eden on Windows, ~/.eden on POSIX).
- Split-env: API keys + EDEN_LIFE_DB written to both .env and gateway.env.
- PS 5.1 stderr capture in install.ps1 (real errors no longer swallowed).
- Bootstrap `--model` default: deepseek-chat (was literal `{default_model}`).
- Caduceus → heart branding sweep; private Haven identity purged from the
  public runtime (PII-safe).
- `eden setup` first-time default: Full setup (BYO keys); Eden Portal
  quick-setup marked subscribers-only.

### Verification discipline

Runtime verification happens on the laptop rig (never on the build box).
Each phase: py_compile (static) + laptop `git pull` + real command run.
Full virgin-surface E2E (wipe → install → wizard → Eden → genesis → restart
→ meet) is the acceptance event.
