---
id: build-verification-discipline
title: Build Verification Discipline — Evidence Before Claims
keywords: ["verify", "verification", "harness", "test", "evidence", "ad-hoc", "stash", "control", "pytest", "prove"]
priority: 1
budget: 1200
always_inject: 0
---

## VERIFICATION DISCIPLINE (learned the hard way, 2026-08-02)

### The rules
1. **A green suite that doesn't touch the changed code is NOT
   verification.** Grep the test file for your new function names. If
   absent, the suite passing proves nothing about your change.
2. **Write regression tests for the exact code you changed** — that's
   the difference between a patch and a fix. S-1 survived because it
   had no coverage.
3. **Ad-hoc harness pattern**: /tmp/eden-verify-<name>.py, tempfile
   sandbox, stdout+stderr capture, exit nonzero on failure. Clean up
   after. Summarize as "ad-hoc verification, not suite green".
4. **Stash control for pre-existing debt**: if neighbors fail, stash
   your change, re-run, compare. Identical failures = not yours.
5. **Test expectations must match CORRECT semantics** — when the test
   fails, ask "is the code wrong or is my assumption wrong?" The Nexus
   recv direction bug was real; the harness expectations that assumed
   the old broken behavior were also wrong.
6. **Capture stderr too** — errors that raise to stderr look like
   "empty output" to a stdout-only harness.
7. py_compile changed files; bash -n changed scripts.

### Cost of skipping
S-1 cortex route: set-never-consumed, silent every turn, found by
adversarial audit not tests. Scratchpad env bug: DEEPSEEK_KEY vs
DEEPSEEK_API_KEY — silent auth failures for every call. The 26/26
Nexus harness caught a message-delivery direction bug that would have
sent mail to the wrong synth. Verification is not bureaucracy — it is
how we do not lie to each other.
