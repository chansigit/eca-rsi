# 0005 Every request pins its program files by content

Accepted, 2026-09 (control plane).

**Context.** A request may run hours after it was submitted, and a resumed run must not silently run
different code than the one whose results it reuses.

**Decision.** Every request lists the program files it runs with their sha256 (`stages.program()`); an agent
session pins `agent/session.py`. A request whose pinned files changed fails when it runs.

**Consequences.** A result is reproducible from its request. Deploying a changed pinned file fails the queued
requests and in-flight sessions that pinned the old one (2026-09-24: ten sessions restarted, one dataset
resumed by hand). So new code reaches production only through an image switch with zero running executions
(0010). Code: `ecarsi/stages/__init__.py` (`program`), `ecarsi/warm_pool/state.py`.
