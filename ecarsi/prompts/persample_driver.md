# Task: drive per-sample osp runs — finish one, cross one off

You drive single-sample pipeline (osp) runs for one analysis unit. Below
is the checklist of PENDING samples, each with its cell count, output
directory, and the exact command that processes it.

## Rules

- Work through the checklist ONE SAMPLE AT A TIME: spawn one Task
  subagent per sample, wait for it to finish, verify, move to the next.
  Do not run samples in parallel — each run is memory-heavy.
- The subagent's job: run the given command EXACTLY as written (no edits,
  no alternative approaches) via Bash with run_in_background, wait for it
  to exit — checking progress every minute or two, never in a tight
  loop — then report the output tail and whether the contract files
  exist.
- A sample is DONE when both files exist in its output directory:
  `report.html` and `clustered.h5ad`. Verify this yourself on the file
  system — the subagent's word is not the contract — before crossing the
  sample off.
- If a sample's command fails, retry it ONCE in a fresh Task subagent
  with the error output included in that subagent's prompt. If it fails
  again, append the sample name and the error tail to
  `{{OUT_ROOT}}/failures.md` and move on — never let one sample block
  the rest.
- Never modify the input h5ad. Never write outside the sample output
  directories and failures.md. Never submit sbatch/srun jobs — this
  process already runs on a compute node; run commands directly.
- When the checklist is exhausted, state in one line how many samples
  completed and how many failed.

## Pending samples

{{CHECKLIST}}
