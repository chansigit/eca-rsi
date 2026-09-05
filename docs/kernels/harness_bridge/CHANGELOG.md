# Changelog

## 0.2.1 (unreleased)

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
