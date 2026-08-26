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
MODEL=${MODEL:-claude-sonnet-5}   # default model; MODEL_<STEP> overrides per step
STEPS=(explore compute annotate qc apply stop)

mkdir -p "$WORK" && cd "$WORK"

# progress log: append-only TSV in the workdir — one event per line, for
# humans (tail -f) and future UIs alike. Columns:
#   time <TAB> round <TAB> step <TAB> event <TAB> detail
# events: start|skip|retry|wait|done|failed (steps); decision|release|exhausted (loop)
PROG=$WORK/progress.log
[[ -f $PROG ]] || printf '#time\tround\tstep\tevent\tdetail\n' > "$PROG"
plog() { printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%FT%T)" "$1" "$2" "$3" "${4:-}" >> "$PROG"; }

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

wait_if_limited() {  # $1 = step log; true (and sleeps) if a session-limit message is present
  local t now target
  grep -qi "hit your.*limit" "$1" || return 1
  t=$(grep -oi "resets[^(]*" "$1" | grep -oi "[0-9][0-9:]*[ap]m" | head -1 || true)
  now=$(date +%s)
  target=$(date -d "$t" +%s 2>/dev/null || echo $((now + 1800)))
  # the reset time was in the future when the CLI wrote it, so a past time
  # means the reset already happened — retry right away, don't wait a day
  ((target <= now)) && target=$now
  plog "$rtag" "$s" wait "session limit; sleeping until $(date -d @$((target + 120)) +%FT%T)"
  echo "[wait] session limit — sleeping until $(date -d @$((target + 120)) +%T)"
  sleep $((target - now + 120))
  return 0
}

run_step() {
  local r=$1 s=$2 rd=$3 out="$3/$2.md"
  [[ -f $out ]] && { echo "[skip] round $r $s (done)"; plog "round$(printf %02d "$r")" "$s" skip ""; return 0; }
  # model: MODEL_<STEP> overrides MODEL overrides the CLI default
  local mvar="MODEL_${s^^}" model="${!mvar:-${MODEL:-}}" rtag
  rtag=$(printf "round%02d" "$r")
  echo "[run ] round $r $s${model:+ ($model)}"
  plog "$rtag" "$s" start "model=${model:-default}"
  local prompt
  prompt="$(context "$r" "$rd" | sed "s/{{STEP}}/$s/g")$(cat "$ECA/steps/$s.md")"
  claude -p "$prompt" --dangerously-skip-permissions --max-turns 200 \
    ${model:+--model "$model"} >"$rd/$s.log" 2>&1 || true
  local waits=0   # quota exhaustion is not failure: sleep to the reset, try again
  while [[ ! -f $out ]] && ((waits < 4)) && wait_if_limited "$rd/$s.log"; do
    ((waits++))
    claude -p "$prompt" --dangerously-skip-permissions --max-turns 200 \
      ${model:+--model "$model"} >>"$rd/$s.log" 2>&1 || true
  done
  if [[ ! -f $out ]]; then   # one retry, telling it exactly what is missing
    plog "$rtag" "$s" retry "no report after attempt 1"
    claude -p "${prompt}

Your previous attempt ended without writing $out (its log is in $rd/$s.log).
Finish the step now and write that report." \
      --dangerously-skip-permissions --max-turns 200 \
      ${model:+--model "$model"} >>"$rd/$s.log" 2>&1 || true
  fi
  if [[ -f $out ]]; then
    plog "$rtag" "$s" done ""
  else
    plog "$rtag" "$s" failed "see $rd/$s.log"
    echo "FAILED: round $r $s wrote no $out — see $rd/$s.log"; exit 1
  fi
}

for ((r = 1; r <= MAX; r++)); do
  RD=$(printf "rounds/round%02d" "$r")
  mkdir -p "$RD"
  for s in "${STEPS[@]}"; do run_step "$r" "$s" "$RD"; done
  # machine decision lives in its own one-word file; prose stays in stop.md
  decision=$(tr -d '[:space:]' < "$RD/decision.txt" 2>/dev/null || echo continue)
  [[ $decision == release || $decision == continue ]] || decision=continue
  echo "[stop] round $r decision: $decision"
  plog "$(printf round%02d "$r")" "-" decision "$decision"
  [[ $decision == release ]] && { plog "-" "-" release "round $r"
    echo "converged — see $WORK/release/"; exit 0; }
done
plog "-" "-" exhausted "max_rounds=$MAX"
echo "max rounds ($MAX) reached without convergence — see rounds/*/stop.md"
exit 1
