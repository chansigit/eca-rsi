# 0001 Kernels compute, agents make narrow decisions, the host checks every decision

Accepted, 2026-09 (ecarsi 0.1.0).

**Context.** The first design (branch `primitive`, August 2026) let agents write their own analysis code
through a six-step prompt loop (Explore → Compute → Annotate → QC → Apply → Stop). Results could not be
reproduced, and removed cells could not be traced to a reason.

**Decision.** The kernels (osp, msp, zmip) do all computation deterministically. Agents only submit
structured decisions through tools: which samples enter, what a cluster is, which cells to keep or remove,
which lineages to zoom into. Host code validates every submission against the evidence and rejects it with
a reason the agent can act on. Every removal is one row in a per-cell ledger.

**Consequences.** Computation is reproducible and every decision is auditable. Agent quality matters only
inside narrow choices, which makes models comparable (eca-rsi#14). A new kind of decision needs a tool and a
validator. Biological doubt never blocks a release: it goes to `needs_review` (README, Scientific rules).
Code: `ecarsi/stages/*` (validators), `ecarsi/stages/contract.py` (shared tool contract).
