# 0012 One execution path: the local path was removed

Accepted, 2026-10-02 (ecarsi 0.4.0).

**Context.** Until 0.4.0 the package also carried the local path (`eca-rsi run`: one process, one dataset,
kernels as subprocesses), unmaintained since the control plane took over, with its own resume, pruning,
mirroring and identity digest. Two paths doubled what a reader had to understand, and the docs described the
local one as if it were current.

**Decision.** Remove it: about 3,900 lines of package code and 2,000 of tests, found by a reachability walk
from the code the control plane runs. Pages still read its run layout, because its old runs are served from
their display zones.

**Consequences.** One way to run, one layout to document. Three behaviours that only the local path had are
not on the control plane: explicit sample maps (`merges`, `exclude_cells`, `batch_key`), study-design context
for the agents, and skipping rejected ECA-PP sources. Each needs its own decision. See CHANGELOG 0.4.0.
