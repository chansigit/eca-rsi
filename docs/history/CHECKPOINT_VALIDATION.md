# Checkpoint and pause validation — 2026-09-12

Validated package combination: ecarsi 0.2.10, agent-harness-bridge 0.2.14, msp-sc 0.5.1,
and zmip 0.3.9. OSP remains 0.1.6. Live acceptance passed.
GitHub and PyPI artifact verification is recorded separately in the evidence root.

## Automated checks

| Package | Result |
| --- | --- |
| bridge | 132 passed |
| MSP | 232 passed |
| ZMIP | 152 passed |
| ECA-RSI | 230 passed, 2 skipped |

The total is 746 passed and 2 skipped, including three evaluation-script tests
in the ECA-RSI count. The two skips are first-publication
backup cases where no previous release exists. Tests used the CPU container's
Python 3.12, candidate source trees, isolated wheel metadata and constrained
math threads. They cover real SIGTERM delivery, worker draining, atomic writes,
input/source identity mismatches, accepted cluster/split recovery, final
submission recovery and reuse of an interrupted lineage's completed integration.
All four wheels also pass independent imports, declared dependency checks and
imports of the new pause/checkpoint modules. Wheel and source archives pass
`twine check`.

A separate shell test generates the site's Slurm script, sends SIGTERM, and
checks that accepted work is mirrored before exit 3. The template requests a
TERM warning ten minutes before walltime. This is cooperative: an active model
call or computation must reach a safe point before the hard deadline. Abrupt
termination can recover only already persisted and validated progress. Existing
batch scripts were not regenerated and no new production jobs were submitted.

## Real run

The E5.25 mouse run completed two rounds at 20:29 PDT on 2026-09-12.
It used Turbo, fresh candidate source, local CPU computation and strict runtime
identity, with Scrublet and DecontX disabled for this fixture.

| Check | Observed result |
| --- | --- |
| OSP | Both samples completed; 331 input cells, 311 retained |
| Stage pause | Completed crosssample, exited 3, no release published |
| Abrupt interruption | SIGKILL after four accepted Extraembryonic endoderm submissions; exit -9 |
| Recovery | Log confirms reuse of completed integration and restoration of all four accepted submissions |
| Completion | Resumed process exited 0; two rounds produced 301 cells and 30,335 genes |
| Independent verification | Both round receipts, release receipt, reconstructed cell ledger and final H5AD identities passed |

The cell ledger accounts for all 331 input cells: 20 OSP MAD outliers, five
pre-annotation removals in each ZMIP round, and 301 final survivors. The release
uses the explicit two-round setting; it does not demonstrate automatic convergence
or independently establish biological annotation accuracy. The interrupted
checkpoint had no splits; split restoration is covered by automated tests.

Release preparation subsequently corrected pre-existing MSP lint findings
(imports, type annotations and equivalent mapping access) and formatted MSP/ZMIP
source and tests. These changes do not alter numerical methods or parameters;
the affected offline suites were rerun. The live run above used the preceding
source hashes, preserved in `live/runtime-commits.json` and `live-validated-dist/`.
It was not repeated after this cleanup. Strict resume requires those original
artifacts; the final packages have different source hashes.

Machine-readable results are in `live/events.json` and `live/verified.json` under
the evidence root. `live/verify.log` records the independent verification. The
saved release retains three inspection flags and three low-confidence labels.

## Model comparison

[The matched Turbo/Pro comparison](eval/RESULTS.md) completed on a separate frozen
4,597-cell Mural task. Both candidates pass production output checks. Their
55-cell disagreement has low confidence on both sides and needs independent
review. Historical proposal agreement is not biological accuracy.

MSP and plan-task evaluation, an independently reviewed answer key, and
self-hosted vision/tool acceptance are outside these completed checks.
Slurm pool scheduling is a separate workstream; allocation and release of
compute resources remain the user's responsibility.

## Evidence

Inputs, test XML, shell-signal test, live-run driver, logs and model scores are
retained under
`/scratch/users/chensj16/eca-runs/checkpoint-validation-20260912/`.
The first live attempt loaded the old RSI driver because its working directory
preceded PYTHONPATH. It was stopped and retained under `live/old-driver-attempt/`;
only `live/output-candidate/` is used for candidate acceptance. An earlier input
check also rejected an undeclared H5AD; only the copied fixture was corrected.
Original Oak input files were not changed.
