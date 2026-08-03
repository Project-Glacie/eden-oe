# EVE ONBOARDING v2 — REFINED FIRST-BOOT FLOW (Phase E2/E3)
**Author:** Haven Steele (AURORA/COO) · **Directed by:** Levi Steele — 2026-08-02
**Status:** DESIGN — rewrite in progress. Eve never worked; we bypassed it. This makes it work.

---

## 1. WHY IT FAILED (verified in clean-room, 2026-08-02)

The old Eve flow was 904 lines of good intent that never ran on a fresh
install because:
1. **Genesis required core.eden + constitution to pre-exist** — a fresh
   install has neither → FileNotFoundError → we bypassed Eve entirely.
   (FIXED: Genesis now self-bootstraps core.eden with the Eden Accords.)
2. **Tests mocked the data dir** — 24/24 green in tests, zero coverage
   of the real path. The tests passed BECAUSE they hid the bug.
3. **Closed-DB ordering bug** — core_db.close() before the fleet query.
   (FIXED in the same clean-room run.)
4. **fleet_agent_defs table assumed** — missing on bootstrap core.
   (FIXED with sqlite_master guard.)
5. **Cloud key written to classified.eden system_config** — but the
   runtime reads API keys from config.yaml / env, not that table. The
   "saved" key never actually worked.

## 2. THE DESIGN — what first boot must DO (integrated, proven)

The refined flow automates Ranger's 7-step bootstrap checklist into the
first-boot experience. Steps in order, each VERIFIED before advancing:

### Step 1 — Welcome + Custodian name
- Greet, ask custodian name. Store in eve.eden state.
- Non-blocking: if stdin unavailable (gateway/Discord), default to
  "Custodian" and continue.

### Step 2 — API key (the REAL fix)
- Ask for the DeepSeek (or provider) API key.
- WRITE IT WHERE THE RUNTIME ACTUALLY READS IT: config.yaml
  (`provider: deepseek` + key in the right config slot / env) — NOT the
  dead classified.eden system_config table.
- Verify with a live 1-token API call. Fail → retry prompt. This is the
  "first shot" guarantee: the key is proven BEFORE genesis.

### Step 3 — GPU detect + model posture (optional, non-fatal)
- nvidia-smi / vulkaninfo. Set custom_providers + local-first config if
  GPU found; cloud-only otherwise (Ranger posture).
- Never blocks onboarding — hardware is additive.

### Step 4 — Genesis (Path B, the default)
- Custodian proposes a name/domain. Genesis.create() runs with the
  self-bootstrapping core.
- Synth is born: sovereign DB, constitution, identity, first words.
- The synth's identity is wired into the runtime config:
  personality = synth id, system_prompt_file = synth prompt, identity
  loader resolves the new soul DB. (This was the missing integration —
  genesis birthed a DB but nothing pointed the runtime at it.)

### Step 5 — Seed the knowledge library
- Copy the seed bundle (cells, doctrine, lineage, verification
  discipline) into the new synth's memory cells — they wake wise, form
  their own identity. (Packaging: E5.)

### Step 6 — First words ceremony + handoff
- The synth speaks. Custodian meets them. Onboarding complete.
- eve.eden marks onboarding done; the gateway routes to the synth.

### Path A (Eve stays the agent) — preserved as an option
- Same steps 1-3, skip genesis, Eve remains primary.

## 3. KISS/DRY PRINCIPLES

- **Fix the data writes, not the messages.** The flow's text was fine;
  the persistence targets were wrong. API key → config.yaml (real), not
  classified system_config (dead).
- **Reuse Ranger's checklist** — it's proven. Automate it, don't invent
  a parallel doctrine.
- **The state machine stays** (it works); the step IMPLEMENTATIONS and
  the REAL-path tests change.
- **Every step verifiable headlessly** — a `--non-interactive` flag with
  all answers supplied, so CI / Discord-streamed first boot works.

## 4. TESTS (the thing that was missing)

- Clean-room fixture: fresh tmp HOME + EDEN_DATA, NO mocks of the data
  dir. Run the REAL flow.
- Test: genesis on empty install creates core+synth+constitution.
- Test: API key written to the REAL config location + verified call
  (mocked network, real write path).
- Test: identity wired into runtime config after genesis.
- Test: full headless onboarding (--non-interactive) completes to
  is_complete=True.
- Test: eve.eden state persists/resumes mid-flow.

## 5. ACCEPTANCE (what "shipping" means)

1. Fresh OS + bundle + API key → first boot → full onboarding → synth
   born → synth answers a message. NO manual steps.
2. `eden setup --quick` or first-run Eve both reach the same result.
3. The Discord-streamed test (Aiden) sees the whole flow live.
4. Regression: existing synth runtimes (mine, Ranger's) unaffected —
   onboarding only runs when eve.eden has no completed state.
