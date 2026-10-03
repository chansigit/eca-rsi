# 0012 One execution path: the local path was removed

Accepted, 2026-10-02 (ecarsi 0.4.0).

**Context.** Until 0.4.0 the package also carried the local path (`eca-rsi run`: one process, one dataset,
kernels as subprocesses), unmaintained since the control plane took over, with its own resume, pruning,
mirroring and identity digest. Two paths doubled what a reader had to understand, and the docs described the
local one as if it were current.

**Decision.** Remove it: about 3,900 lines of package code and 2,000 of tests, found by a reachability walk
from the code the control plane runs. Pages still read its run layout, because its old runs are served from
their display zones.

**Consequences.** One way to run, one layout to document. The removal showed three behaviours only the
local path had. Two came back on the control plane in 0.4.1: explicit sample maps (spec `organize.sample_map`,
with `batch_key: false` for one batch) and skipping rejected ECA-PP sources. The study-design context for the
agents stays off until its effect is measured. See CHANGELOG 0.4.0 and 0.4.1.
