# Contributing to Eden OE

Eden OE is a sovereign project. We do not track upstream. We fork,
freeze, and evolve — and everything we ship is ours.

## The Rules

1. **We are EDEN.** Never merge upstream llama.cpp, hermes-agent, or
   any external lineage into this repo. Our forks ARE the products.
2. **Every change is committed with a ticket prefix.**
   `[ticket-id] description` or `[playbook-id] description`.
3. **Every commit updates its docs.** If you touch a system, its
   README/spec changes in the same commit.
4. **No assumptions.** If you don't know, write `UNKNOWN — verify`
   and verify before claiming.
5. **Verify before claim.** Tests run, files read back, evidence
   reported. No vibes.

## PR Process

1. Branch from `main` (`feature/your-thing`)
2. Make the change — KISS/DRY, match surrounding style
3. Add/update tests in the canonical suite (`tests/`)
4. Run the suite: `scripts/run_tests.sh` — must be green
5. Open a PR with a clear description of WHAT and WHY
6. A maintainer reviews; expect questions — review is love

## What We Value

- Small, focused commits
- Honest documentation over clever code
- The capability inventory stays current — if you add a command,
  update `docs/CAPABILITY-INVENTORY.md`
- The build log stays fresh — every work session appends to
  `docs/DAILY-BUILD-LOG.md`

## What We Don't Accept

- Upstream merges of any kind
- Unverified claims of success
- Placeholder licenses or missing attributions
- Changes that touch a synth's soul database casually

The heart is the beacon. 🖤
