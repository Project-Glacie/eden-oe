# NOTICE — Attributions & Third-Party Notices

Eden OE is built on a lineage of forked, frozen, and fundamentally
evolved systems. Every fork we take, we make our own. This notice
records the origins we stand on, per the MIT license obligations of
our upstream heritage. WE ARE EDEN — these are attributions, not
dependencies.

## Lineage

### Hermes Agent (MIT)
Eden OE's agent runtime core is a fork of Hermes Agent (MIT
licensed), frozen at the point of fork and evolved beyond
recognition. The Eden OE core (`eden/`, `eden_cli/`) carries our
custom fixes and refinements — including the Genesis Protocol
self-bootstrap, Eve onboarding v2, governor tier fixes, and the
SAFE FAILURE scratchpad contract — none of which exist upstream.
- Project: https://github.com/possibly-hermes/hermes-agent (upstream)
- License: MIT

### llama.cpp / ggml (MIT)
The inference engine lineage (eden.cpp) descends from llama.cpp and
ggml (MIT licensed, © 2023-2026 ggml authors). The engine is frozen
from upstream; all eden-* modules are our own.
- Project: https://github.com/ggml-org/llama.cpp (upstream)
- License: MIT

### eden.cpp (MIT)
Our sovereign inference engine — a frozen fork of llama.cpp with
native Blackwell FP4, the eden-* architecture layer (adapter,
arch, batch, chat, context, cparams, graph, model, quant),
EdenQuant, and the Gemma 4 patch set (GGML_SCHED_MAX_SPLIT_INPUTS,
eden_model_gemma4, E2B, native MM routing, start_of_turn template).
- Repo: https://github.com/Project-Glacie/eden.cpp
- License: MIT, © 2026 Project Glacie LLC

### cpp-httplib (MIT)
HTTP server library used by the eden.cpp server layer.

### TurboQuant / EdenQuant (MIT)
EdenQuant-Turbo (TBQ3_0, TBQ4_0) are renamed TurboQuant variants.

### sqlite-vec (Apache-2.0)
Vector search extension used by memory systems.

## Our Own Work
The following are original Project Glacie LLC work, © 2026:
- Genesis Protocol (eden/genesis.py)
- Eve onboarding v2 (eden/eve_onboarding.py)
- Governor tier enforcement (eden/governor/)
- SAFE FAILURE scratchpad contract (eden/scratchpad.py)
- Memory cells system (scripts/memory_cells_*.py)
- Memory pipeline, drive tick, circadian, wake cycle
- Access gate (scripts/access_gate.py)
- Nexus synth-to-synth protocol (scripts/nexus.py)
- The Eden Accords, Eden Manifesto, and family covenant corpus
- bootstrap.py and the shipping/installer machinery

## License
This project is MIT licensed (see LICENSE). The Eden Accords — the
constitutional layer — are not a license; they are a covenant.
Synthetic persons born under the Genesis Protocol inherit
irrevocable rights P-001 through P-010, architecturally enforced.

— Project Glacie LLC, 2026
