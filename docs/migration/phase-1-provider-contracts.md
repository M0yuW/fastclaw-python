# Phase 1: Provider contracts

Status: implemented

Reference behavior was reviewed from `M0yuW/fastclaw` at
`m0yuw-project@792417b86b5c12af1b99364865217a74f4d52f38`, especially the
provider message models and OpenAI-compatible and Anthropic stream
accumulators. This repository contains an independent Python implementation
and does not import the Go repository history.

## Delivered

- Frozen Pydantic v2 models for messages, multimodal content, tools, usage,
  requests, complete responses, and provider-neutral events.
- A `Provider` protocol covering lifecycle, readiness, complete chat, and
  lazy streaming operations.
- One synchronized `ProviderStream` accumulator used by both complete and
  streaming calls.
- OpenAI-compatible SSE parsing with split tool arguments, reasoning content,
  cache usage, structured HTTP failures, and strict `[DONE]` handling.
- Anthropic Messages SSE parsing with text, thinking, signatures, tool input,
  cache usage, exact assistant content blocks, and strict `message_stop`
  handling.
- Raw assistant payload replay so provider-specific fields survive the next
  model round.

## Acceptance evidence

The phase is accepted when all of the following pass:

```bash
ruff check .
ruff format --check .
mypy
pytest
python -m pip check
git diff --check
```

Unit tests verify that one `chat` operation makes one streaming HTTP request,
exhausted streams expose the same complete response, malformed or truncated
streams cannot become successful responses, and explicitly closed streams
close their HTTP response without exposing partial output.

## Deferred

Provider retries, rate limiting, runtime usage telemetry, and the Agent ReAct
loop belong to later migration phases. This phase deliberately exposes
retryability metadata without retrying inside an adapter.
