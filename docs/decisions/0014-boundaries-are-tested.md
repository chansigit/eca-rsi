# 0014 Subsystem boundaries are written down and tested

Accepted, 2026-10-02.

**Context.** The six parts ([OVERVIEW](../OVERVIEW.md)) were joined by habit, not by declared interfaces.
The stage programs called some 27 private functions of the kernels, so a kernel refactor could break a
pool task without any test in either repository noticing. The JSON files the subsystems hand each other
(publications, display records) had their format only in the code on both ends; a field one side
dropped surfaced stages later as a `KeyError`.

**Decision.** Three written boundaries, each enforced by a test:

- **Kernels.** osp, msp and zmip each have an `api` module listing every name eca-rsi uses, under a public
  name (private ones are aliased there). eca-rsi imports a kernel only through it, and only from the
  stage programs (`ecarsi/stages/`); the control image has no kernels.
  `tests/test_layers.py` refuses any other kernel import; each kernel's `tests/test_api.py` checks that
  every name resolves. Changing a name in an `api` module is a breaking change for eca-rsi.
- **Directions.** `tests/test_layers.py` also lists which subsystem may not import which: the pool knows
  nothing of orchestration, agents or science; agents and stage programs never import orchestration;
  Periscope never imports orchestration, agents or stage programs. Known exceptions are listed with
  their reason.
- **Shared files.** `ecarsi/contracts.py` names the fields each shared JSON file must carry (display
  record, per-sample, stage, round, unit and dataset publications, degraded records). Writers check
  before writing, readers after reading. A file without `schema` is version 1.

**Consequences.** A kernel can change anything outside its `api` module freely. A new cross-boundary
import or a dropped field fails a test with the place named. The contracts list only what readers rely
on, so old runs keep passing. Since 0.4.3 the stage-only helpers live in `ecarsi/stages/`, the generic
file helpers every part uses moved from `warm_pool/state.py` to `ecarsi/files.py`, and the modules left directly
in `ecarsi/` are the shared vocabulary, which may import no part (two listed exceptions: `display`, `observatory`).

**Amended 2026-10-06 (decision 0018).** The kernels and harness_bridge are packages of this repository; the image snapshot carries them, and `tests/test_layers.py` also says what each of them may import.
