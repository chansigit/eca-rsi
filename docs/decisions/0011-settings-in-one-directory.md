# 0011 Every setting in `~/.config/ecarsi`, machine paths only in `deployment.env`

Accepted, 2026-10-02.

**Context.** Settings were spread over a launcher script, four ops scripts with hard-coded image paths, and
files with unclear names (`storage.json`, `periscope.json`, `registry.json`, `model-pool.json`). Moving to
another machine meant hunting for paths.

**Decision.** Everything the owner edits lives in `~/.config/ecarsi/`: `deployment.env` (images, state
directory, mounts, host Python, ports: everything machine-specific), `results.json`, `models.json`,
`periscope-datasets.json`, `temporal.yaml`, and the secrets `periscope-password` and `models-admin-key`.
Every launcher and ops script reads `deployment.env`. The package itself names no machine path; `BINDS` is
required.

**Consequences.** Porting is editing two files and initialising the pool and the agent service. Service-owned
files (`pool/config.json`, `bridge/config.json`) are written by their tools. Table: INSTALL.md A.4; template:
`examples/deployment.env`.
