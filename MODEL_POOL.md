# Agent model pool in Periscope

Open **Agent models**, below **Warm pool**, to see the primary model followed by
Fallback 1, Fallback 2, and so on. The panel stays inside Periscope and works
independently of Slurm workers.

The inventory reads only `ECA_MODEL_CATALOG` (default
`~/.config/ecarsi/model-pool.json`). It never adds models from environment
variables, bridge defaults or provider discovery. A missing file shows an
empty inventory. Existing `candidates` strings are still readable; saving in
the editor migrates them to the explicit `models` array.

```json
{
  "source": "User model configuration",
  "models": [
    {"harness": "openai", "model": "my-primary", "url": "https://provider.example/api/v3"},
    {"harness": "openai@vllm", "model": "my-local-model", "url": "http://worker:8000/v1"}
  ],
  "validation": []
}
```

**Edit models** adds or removes entries, changes model names and API base URLs,
and moves entries up or down. The first entry is primary. Models sharing one
backend must share its URL because the bridge configures the endpoint per
backend. URLs cannot contain credentials, query parameters or fragments.
Saving is atomic and checks the loaded revision; a concurrent change requires
reload. No running job's routing changes. Copy the panel's **Launch exports
for new drivers** into the shell that will start the driver to use these
settings. The catalog is not a global model scheduler.

Public writes and API-key presence checks require administrator access. A
server started with `--auth` uses its existing authentication. Otherwise the
model settings have a separate management key, in `model-admin-token` beside
the catalog. Provision a random key of at least 32 characters in a file readable
only by its owner. Enter it in **Management key** after pressing Edit or Check.
The key is held only in page memory, never placed in a URL or local storage.
Local requests retain Periscope's existing local-admin access. Model-admin
access does not grant Bind/Unbind access.

**Check API keys** reports two separate facts: whether `~/.bashrc` contains a
direct, non-placeholder assignment and whether the current web process has
inherited the variable. It never returns key values, executes bashrc, follows
sourced files or calls a provider. Expressions such as `$(...)` are reported
as indirect, not evaluated. Presence is not proof of valid credentials or quota.

| Backend | Key variable | URL variable for new drivers |
| --- | --- | --- |
| `openai` (Ark) | `ARK_API_KEY` | `DOUBAO_BASE_URL` |
| `openai@openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_BASE_URL` |
| `openai@vllm` | `VLLM_API_KEY`, only if required by the server | `VLLM_BASE_URL` |
| `claude` | Claude Code login | Managed by the CLI |
| `deepseek` | dsh configuration | Managed by the CLI |

The panel shows the corresponding `export KEY='PASTE_REAL_KEY_HERE'` lines.
Fill real keys in bashrc on the driver host, then run `source ~/.bashrc` before
starting new processes. Existing services retain their previous environment.
API keys do not belong in this catalog or its URLs.

Optional validation entries contain `candidate` (`harness:model`),
`checked_at` and `scope`. Records outside the chain appear as **Validated
alternatives**. Earlier success is not current endpoint health or a biological
accuracy claim. The view does not monitor concurrent calls or account quota.
The public inventory refreshes every 30 seconds except during editing.
`/_models/status.json` and the administrator-only `access`, `save` and `keys`
POST endpoints all disable caching; existing Periscope authentication also
applies. Errors do not echo raw configuration or credentials.
