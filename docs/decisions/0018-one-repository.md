# 0018 One repository: the kernels and the agent harness live in eca-rsi

Decided 2026-10-06 by the owner.

**Context.** osp, msp, zmip, standissect-lite and agent-harness-bridge were split out as packages to make early
development easier. By October 2026 only eca-rsi used them (eca-pp keeps its own harness copy), and the split cost
more than it bought:

- A fix across a kernel and eca-rsi needed a kernel commit, a version bump, a wheel built by hand, an image rebuild
  that swapped the wheel, and a version sync in eca-rsi (the stress policy of 0017 and msp 0.5.4's DEG fix both went
  that way). Several open issues (#27, #28, #29) cross the same line.
- Nothing recorded which kernel source an image held: the images carried wheels, `BUILD.json` named only the
  eca-rsi commit.
- The kernels kept standalone agent flows, prompts and guards that production never runs, and their rules drifted
  from eca-rsi's (#28).

**Decision.**

- **One repository.** `osp/`, `msp/`, `zmip/`, `standissect_lite/` and `harness_bridge/` sit at the top of eca-rsi
  next to `ecarsi/`, with their histories (rewritten to these paths and merged, so `git log <path>` reaches their
  first commits). Import names are unchanged. Their tests are in `tests/<package>/`; their READMEs, changelogs and
  old packaging files in `docs/kernels/<package>/`.
- **One version.** eca-rsi's version (`pyproject.toml`) is the project's. The merged packages keep their last release
  number as a constant (msp 0.5.4, harness_bridge 0.2.15, standissect_lite 0.2.0); the GitHub repositories are
  archived with a pointer here, and nothing is published to PyPI any more.
- **Images carry the source.** The snapshot at `/opt/eca-rsi` is `git archive` of all six packages; the build takes
  the old wheels out of `/opt/rsi-python` and `/opt/rsi-control` and checks that every package is imported from
  `/opt/eca-rsi`. Wheels swapped into an image are third-party only.
- **Boundaries stay, enforced by tests rather than by packaging.** `tests/test_layers.py` keeps "only
  `ecarsi/stages/` uses the kernels, through their `api` modules" (0014) and adds a table of what each merged package
  may import: no kernel and not harness_bridge imports `ecarsi`; osp may use harness_bridge; msp standissect_lite and
  harness_bridge; zmip msp and harness_bridge.

**What it costs.** The repository is larger (about 18,000 more lines and 290 more tests; the full suite runs them
in the compute image). zmip's compute identity changed once at the first image built this way (the packages lost
their distribution metadata), so lineage caches from before were invalidated. zmip still reaches into msp's modules
beyond `msp.api`; tighten that when the standalone flows go (#28).

**Update 2026-10-06 (#28).** The standalone flows are gone: about 4,800 lines of kernel code (command lines, agent
flows, the zmip lineage runner, msp's dask endpoints and agent checkpoints) and 2,700 lines of their tests, plus the
generation-1 `eval/` replay. The kernels keep what their `api` modules reach; none imports harness_bridge any more
(test_layers). The removal reasons are one list, `msp.annotate.REMOVE_REASONS` (now with dissociation and dying, so
cross-sample accepts the reasons 0017's guard already checks there), and the batch guard one function, used by
cross-sample and zoom-in.
