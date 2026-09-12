# 2026-09-12 integration validation

The release combination is ecarsi 0.2.9, agent-harness-bridge 0.2.13,
msp-sc 0.5.0, zmip 0.3.8, and unchanged osp-sc 0.1.6.

## Changes and checks

| Package | Automated tests | Covered changes |
| --- | --- | --- |
| agent-harness-bridge 0.2.13 | 130 passed | Provider-qualified configuration and records; OpenRouter/vLLM adapters; capacity versus account limits; MCP lifecycle |
| msp-sc 0.5.0 | 229 passed | Local/Dask execution, GPU resource routing, integration, explicit coarse-boundary review, corrected submissions and saved outputs |
| zmip 0.3.8 | 151 passed | Shared-island explanations, preserved plan defenses, resume and publication recovery |
| ecarsi 0.2.9 | 218 passed, 7 skipped | Runtime versus agent provenance, original OSP receipt reuse, persistent skipped-check records, review rendering and existing driver contracts |

Tests used Python 3.12 in the existing CPU container environment, all four
candidate source trees on `PYTHONPATH`, and isolated wheel metadata. The seven
RSI skips are retained conditional tests, not successful checks. Wheel-only
imports and declared dependencies were also checked with the extracted wheels
first on `PYTHONPATH`, without replacing the shared environment's dependencies.

The bridge JavaScript syntax check uses Node 24.13.0. Its account-limit test now
advances a simulated clock alongside simulated sleep; this removes a busy loop
from the test without changing production retry timing.

## Boundary replay

A read-only replay covered 19 saved chondroatlas rounds. The previous MSP
local-DEG guard rejected 8 rounds but accepted all three 04_Sunetal rounds, the
case it was intended to address. It compared pooled local DEG evidence, which
cannot establish a pairwise lineage boundary.

The replacement requires explicit evidence and an uncertainty flag for each
adjacent pair of retained coarse labels: 221 reviews across those 19 saved
rounds. Replaying explicit uncertainty preserved all labels. For 04_Sunetal,
the three rounds require 7, 8 and 9 coarse-pair reviews; its third-round ZMIP
plan also requires a written review for `island_1`. A boolean confirmation alone
no longer satisfies that plan contract.

Replay explanations were synthetic contract inputs, not new biological
judgments. No historical proposal, cell label or matrix was modified. The new
rules require an auditable decision; they do not certify that an explanation
is biologically correct or force labels to merge. A full new model-driven run
of this release combination has not been performed.

## Compute scope

Harmony, graph/clustering, and DE support `local`, `dask-local`, and an existing
shared `dask` pool; the three calls have optional RAPIDS GPU implementations.
ZMIP reuses these calls. Earlier real CPU/GPU results are recorded in
[MSP's compute record](https://github.com/chansigit/msp/blob/main/docs/compute-endpoint-design.md).

Pool allocation/startup remains manual. Automatic scaling, a dedicated driver
tier, and OSP offloading are not implemented. Drivers still handle matrices,
preprocessing, standissect, figures and file I/O. CPU/GPU numerical results need
not be bit-identical. Workers must be restarted after their imported code changes.

## Operational handoff

Seven chondroatlas studies already have Oak releases. Their existing outcomes
are unchanged by this integration. Four browser registrations (04_Sunetal,
12_Yangetal, 13_Kodamaetal and 18_Claytonetal) now use the Oak copies. The abandoned
Tabula Sapiens SS2 Small_Intestine registration was removed, and the local batch
registration script excludes that abandoned run; its data were not deleted.
Local and public browser requests returned HTTP 200 after the registry update.

Stress-population policy and a fixed coarse-lineage vocabulary remain deferred.
Self-hosted vLLM adapter support does not establish that the separately queued
local model service has passed a vision/tool run.

## Release artifacts

Each repository's GitHub Release carries its wheel and source archive. PyPI
publication and downloaded-file hashes are verified separately. Exact versions
and install commands are in [INSTALL.md](INSTALL.md); the release-specific
changes are in [CHANGELOG.md](CHANGELOG.md).
