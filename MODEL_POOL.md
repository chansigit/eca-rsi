# Agent model pool in Periscope

Use **Agent models**, below **Warm pool**, to see the primary model followed by
Fallback 1, Fallback 2, and so on. The panel stays inside Periscope. It is
independent of the Slurm compute pool and remains accessible without workers.

The inventory reads `AGENT_MODEL_POOL` from the web process, then an explicit
`HARNESS`/`MODEL`, then `candidates` in `ECA_MODEL_CATALOG` (default
`~/.config/ecarsi/model-pool.json`), otherwise the bridge's single-model default.
This is a displayed configuration, not a global scheduler: individual drivers
may override it. Editing the catalog never changes a running driver's routing.

The optional catalog contains public metadata only:

```json
{
  "source": "My batch launcher default",
  "candidates": "openai:my-primary,claude:my-fallback",
  "validation": [
    {
      "candidate": "openai:my-primary",
      "checked_at": "2026-09-12",
      "scope": "Describe the specific completed RSI check here."
    }
  ]
}
```

Validation records outside the chain appear as **Validated alternatives**.
A successful earlier run is not proof of current account access, remaining
quota, model quality, or endpoint health. This panel makes no inference calls
and does not monitor concurrent calls. No credentials or endpoint URLs are
returned. The authenticated, uncached `/_models/status.json` endpoint reads
configuration afresh; malformed configuration returns a generic 503 without
its raw content. The open panel refreshes every 30 seconds.
