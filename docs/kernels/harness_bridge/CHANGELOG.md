# Changelog

## 0.2.11 - 2026-09-11

- A transient failure that outlives the whole same-backend backoff schedule
  (provider down for hours, not a gateway hiccup) now advances the
  `ModelPool` to the next candidate instead of ending the run; without a
  pool it still raises as before. Usage/rate limits still never fall back.

## 0.2.10 - 2026-09-11

- Bound each Ark HTTP request with an explicit client timeout
  (`OPENAI_AGENTS_REQUEST_TIMEOUT_S`, default 300s) instead of the SDK's ~600s
  default, so a provider outage fails a transient attempt in minutes rather
  than letting one dead request stall `retry_transient` for hours
  (eca-rsi BATCH_RUN_FINDINGS #19).

## 0.2.4 - 2026-09-07

- Every log line is time-stamped and every application tool call is timed (slow-call lines + per-run summary);
  `harness_bridge.logtimes` derives stage and tool durations from such a log.
- Recognize Ark's item-count limit ("Maximum of 1000 items allowed in input") as a recoverable context-limit error.
- Retry provider connection/request timeouts, Ark's non-JSON 400 "Error when parsing request" and 5xx
  InternalServiceError as transient failures.

## 0.2.3 - 2026-09-05

- Derive distribution metadata from the public module version, removing the
  duplicated version declaration. Correct the stale module version in 0.2.2;
  recovery and tool behavior are unchanged.

## 0.2.2 - 2026-09-05

- Recover explicit Responses output-length termination with at most two fresh
  model sessions by default (`OPENAI_AGENTS_MAX_OUTPUT_RESETS`). Preserve host
  tool/task state and validated partial submissions; never accept truncated text.
- Enforce one logical model-turn budget across recovery sessions with public SDK
  hooks, retaining available usage and marking failed-provider usage incomplete.
- Keep content-filter, unknown terminal reasons and unrelated model errors fatal.
  This recovery is in-process only; no cross-process partial checkpoint exists.

## 0.2.1 - 2026-09-05

- Paginate host text Read results with `byte_offset`/`max_bytes`; default 8 KiB,
  maximum 32 KiB, exact UTF-8-safe continuation offsets. Preserve image and
  working-directory confinement behavior. Addresses large barcode tables
  exhausting model context after a single read.

## 0.2.0 - 2026-09-04

- Progress lines go through `logging` (`harness_bridge` logger family)
  instead of `print`. `configure_logging(*names, level=, stream=)` routes the
  bridge and the application's own logger families to one flushed stream;
  `ensure_logging` attaches the default stdout handler when nobody configured
  logging, so existing callers keep seeing the same lines.
- `_env_float` reports an unparsable environment value at `WARNING`.

## 0.1.0 - 2026-08-30

- First release: one submit-tool oriented `run_agent` API over OpenAI Agents
  SDK, Claude Agent SDK and DeepSeek Harness.
