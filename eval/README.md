# ECA-RSI agent evaluation

Is model X good enough to drive this pipeline? Today the only way to answer that is to run a
dataset for a day and look at what came out. This directory turns the runs we have already
paid for into a fixed exam, so a new model can be scored in minutes instead of a day.

## Where the questions come from

Every agent decision the pipeline has ever made is still on disk, in three parts:

| part | where | example |
|---|---|---|
| the question | the diagnostic tables the agent was allowed to read | `zoomin/<lineage>/deg_local_msp_leiden_r2.0.csv`, `cluster_qc_*.csv`, `foreign_signal_*.csv`, `paga_neighbors_*.csv` |
| the answer | the JSON it submitted | `zoomin/<lineage>/annotation_proposal.json`, `crosssample/*_proposal.json`, `zoomin/zmip_plan.json` |
| the verdict | what the host did with it | acceptance/rejection in the slurm log, `zmip_plan.json:host_warnings`, `lineage_islands.csv`, the resulting `*_removed.csv` |

So a fixture is a frozen copy of one round directory's *inputs* plus the recorded outcome. No new
labelling, no new compute.

## What is actually being measured

Three different things that must not be averaged into one number.

### 1. Contract compliance — objective, cheap, no answer key needed

Does the model produce a submission the host accepts? Every one of these has a machine-checkable
right answer, and every one has been seen in production:

- `submit_cluster` with `confidence` outside `('high','medium','low')` (E13.5, 2026-09-08)
- tool arguments that are not valid JSON — `JSONDecodeError: Unterminated string` (E13.5, same round)
- a zmip plan that merges lineages the host's kNN connectivity check found on separate UMAP islands
- a plan that splits one island across lineages without `confirm_shared_islands`
- cell-conservation violations: cells that appear in neither the survivors nor a removal ledger

Score: fraction of submissions accepted on the first try; number of host round-trips needed.
This is the cheapest and most discriminating dimension — a model that cannot reliably fill a
schema cannot drive the pipeline at all, regardless of how good its biology is.

### 2. Judgement quality — needs care, because "correct" is contested

Candidate references, weakest to strongest:

- **agreement with the upstream `cell_type` prior** where one exists (Parse heart_male has 15 real
  labels, Tabula Muris has `cell_ontology_class`). Report as *agreement rate*, never as accuracy:
  RSI is explicitly allowed to disagree with the prior, and sometimes should.
- **agreement with the decision the production run made**, i.e. does model X reach the same
  keep/drop/merge call as the model that actually ran. This measures consistency, not truth.
- **a small curated answer key** — the only real ground truth, and the only part that costs human
  time. Start with the handful of cases where we already know the answer because a human checked:
  the Fu 2022 meniscus conclusions that matched the manual analysis, the 3m-plate exclusion, the
  `qc_too_few_survivors` plates.

Do not let 1 and 2 be summed. A model can be perfectly compliant and biologically useless.

### 3. Robustness and cost — the reason this benchmark exists at all

Measured per task, already emitted by the bridge's run summary:

- model requests, input/output tokens, reasoning tokens
- wall time, tool calls
- `cost_usd` where the backend reports it (claude does; Ark/Doubao does not — see `ecarsi.cost`)
- infrastructure failures encountered: output-length truncation, `PreviousResponseNotFound`,
  usage limits

Note these are *not* model-judgement failures. `PreviousResponseNotFound` is the provider losing a
stored response; output-length truncation is a model that could not finish inside the cap. They
belong here, not in dimension 1, and mixing them would blame the model for the provider.

## Why the bridge makes this cheap

`harness_bridge.run_agent()` already takes `tools`, `submit_tool`, `prompt`, `cwd` and resolves the
backend from `HARNESS`/`MODEL`. A fixture replay is therefore: point `cwd` at a frozen input
directory, hand it the same tool table the kernel would have handed it, run, and compare the
submitted JSON against the recorded outcome. Swapping models is one environment variable; the
tool table and the prompt do not change.

The one thing the bridge does not give us yet is a way to run the *same* task against several
models in one go — see chansigit/agent-harness-bridge#1, which introduces an ordered candidate
list for a different reason (fallback) but touches the same seam.

## Layering

This lives in eca-rsi rather than in the bridge because the fixtures are single-cell domain data
and the tool tables come from osp/msp/zmip. The bridge stays domain-agnostic. If the exam turns
out to be useful beyond ECA, split it out then, not now.

## What exists

- **`baseline.py`** — classifies the host rejections already sitting in the production Slurm logs.
  No API calls. On 354 logs the incumbent (`doubao-seed-2-1-turbo-260628`) shows 23,850 submissions
  with a 3.4% rejection rate, dominated by `format` (1.9%) and `consistency` (0.9%). Useful within a
  model, **not** across models: each was scored on whatever tasks it happened to run.
- **`extract.py`** — freezes one recorded ZMIP lineage decision into a fixture: `inputs/` (the h5ad
  and every diagnostic table, with the annotation outputs removed) plus `answer.json` (how
  `annotate_lineage` was called, what the model submitted, and every rejection the host issued to
  that lineage). Verified on two real lineages.

## Next

`replay.py`: call `zmip.annotate.annotate_lineage()` on a fixture under a chosen `MODEL` and score
the new proposal — host-checkable rules as pass/fail, agreement with the recorded proposal as a
separate number. That is the first thing here that costs money, so pick fixtures deliberately.
