# 0010 Production code is the image snapshot

Accepted, 2026-10-01.

**Context.** Production used to run from checkouts installed editable into shared environments. An edit could
change code under running tasks: a page edit invalidated two computations on 2026-09-07, and a draft in the
live worktree failed 21 tasks in one minute on 2026-09-23.

**Decision.** Two Apptainer images carry all software: the control image (Temporal, PostgreSQL, HQ, agent
SDKs) and the compute image (kernels, numerical stack), both with a snapshot of eca-rsi at `/opt/eca-rsi`.
Editing a checkout changes nothing in production. `CODE=<checkout>` shadows the snapshot for development.
`ops/build-images-update.sh` builds a new pair (stamp `<date>-<n>`); `ops/switch-images.sh` switches only at
zero running executions; `ops/gate.py` then runs one fixed small dataset end to end on the new pair, and the
previous pair stays until it passes (2026-10-02).

**Consequences.** What runs is always a named, verified build. Every fix needs a rebuild and a switch; the
host needs nothing but Apptainer and a Python for the worker launcher. Docs: `container/README.md`, INSTALL.md.
