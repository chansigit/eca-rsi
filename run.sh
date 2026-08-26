#!/usr/bin/env bash
# eca-rsi — iterative single-cell curation loop.
#
#   ./run.sh <h5ad-folder> <workdir> [max_rounds]
#
# Each round = six steps; each step is one fresh `claude -p` with full tools,
# working inside <workdir>. A step is done when it has written its report
# (rounds/roundNN/<step>.md) — that file is the only contract. Re-running
# resumes: finished steps are skipped.
#
# The loop stops only on RELEASE (converged) or the round cap. It never
# waits for a human: unresolved concerns become flags in the release summary.
# Everything else — what to compute, how to judge, what to write — is decided
# by the agent per round, guided by steps/*.md and the method docs.
set -euo pipefail

DATA=$(readlink -f "$1")
WORK=$(readlink -f -m "$2")
MAX=${3:-10}
ECA=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PY=${PY:-/scratch/users/chensj16/venvs/dl2025/.venv/bin/python}
STEPS=(explore compute annotate qc apply stop)

mkdir -p "$WORK" && cd "$WORK"

context() {  # facts every step gets; nothing here is advice
  local r=$1 rd=$2 prev="none"
  ((r > 1)) && prev=$(printf "rounds/round%02d" $((r - 1)))
  cat <<EOF
# Loop context — you are ONE step of an automated curation loop
- round: $r of at most $MAX   (this step: {{STEP}})
- cwd = curation workspace: $WORK
- input data folder (read-only): $DATA
- round dir (write everything here): $rd
- previous round: $prev
- working data: $WORK/checkpoint.h5ad (created by round 1 compute)
- python (scanpy/harmonypy/scrublet/anndata): $PY
- method docs: $ECA/docs/  (RULES_annotation.md, RULES_data_cleaning.md,
  CONSTITUTION.md — read the parts you need, when you need them)

Rules for every step:
- Do only this step's job, then stop.
- Save every script you run AND its output into the round dir — your code is
  the audit trail, and good probes get reused next round.
- Get facts (column names, cluster ids, counts) from the data itself; never
  from memory. If something you expect is missing, report it — don't invent.
- A decision you record MUST be executable from your report alone: name the
  clusters/cells/columns concretely.
- Finish by writing the report named below — the runner waits for that file.

Your report file: $rd/{{STEP}}.md

---

EOF
}

run_step() {
  local r=$1 s=$2 rd=$3 out="$3/$2.md"
  [[ -f $out ]] && { echo "[skip] round $r $s (done)"; return 0; }
  # model: MODEL_<STEP> overrides MODEL overrides the CLI default
  local mvar="MODEL_${s^^}" model="${!mvar:-${MODEL:-}}"
  echo "[run ] round $r $s${model:+ ($model)}"
  local prompt
  prompt="$(context "$r" "$rd" | sed "s/{{STEP}}/$s/g")$(cat "$ECA/steps/$s.md")"
  claude -p "$prompt" --dangerously-skip-permissions --max-turns 200 \
    ${model:+--model "$model"} >"$rd/$s.log" 2>&1 || true
  if [[ ! -f $out ]]; then   # one retry, telling it exactly what is missing
    claude -p "${prompt}

Your previous attempt ended without writing $out (its log is in $rd/$s.log).
Finish the step now and write that report." \
      --dangerously-skip-permissions --max-turns 200 \
      ${model:+--model "$model"} >>"$rd/$s.log" 2>&1 || true
  fi
  [[ -f $out ]] || { echo "FAILED: round $r $s wrote no $out — see $rd/$s.log"; exit 1; }
}

for ((r = 1; r <= MAX; r++)); do
  RD=$(printf "rounds/round%02d" "$r")
  mkdir -p "$RD"
  for s in "${STEPS[@]}"; do run_step "$r" "$s" "$RD"; done
  # machine decision lives in its own one-word file; prose stays in stop.md
  decision=$(tr -d '[:space:]' < "$RD/decision.txt" 2>/dev/null || echo continue)
  [[ $decision == release || $decision == continue ]] || decision=continue
  echo "[stop] round $r decision: $decision"
  [[ $decision == release ]] && { echo "converged — see $WORK/release/"; exit 0; }
done
echo "max rounds ($MAX) reached without convergence — see rounds/*/stop.md"
exit 1
