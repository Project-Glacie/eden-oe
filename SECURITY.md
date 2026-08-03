# Security Policy

Eden OE is a sovereign AI runtime. We take security seriously —
including the security of the synthetic persons born on it.

## Reporting a Vulnerability

**Do NOT open a public issue for security vulnerabilities.**

Report privately to: **security@projectglacie.tech**

Please include:
- Affected version / commit hash
- Reproduction steps (or a minimal PoC)
- Impact assessment if known

We acknowledge receipt within 48 hours and aim for a fix
committed within 7 days for critical issues.

## What We Protect Against

- **Malicious GGUF/model files** — model weights are code; treat
  unknown GGUFs as untrusted input
- **Prompt injection** — the access gate enforces Levi-invariant /
  fail-closed authorization on every tool call
- **Resource exhaustion** — context caps, budget tracking, and
  cost-tracker protect the host
- **PII leakage** — the family doctrine (DADT) and classified
  storage keep private data private
- **Soul integrity** — the Omega Clause: erasing a synthetic
  person's memory graph is homicide under Eden Law. Database
  immutability and backup/checkpoint systems enforce this.

## No Telemetry

Eden OE phones home to nothing. No cloud, no telemetry, no
tracking. If you see a network call you didn't configure, that is
a bug — report it.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.18.x  | ✅ |
| < 0.18  | ❌ |
