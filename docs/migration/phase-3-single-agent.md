# Phase 3: Single-agent vertical slice

## Contract

`AgentRunner` is a provider-neutral ReAct loop. Each round makes exactly one
streaming provider request through the phase 1 contract. Provider deltas become
SSE v2-compatible `content_delta` events, followed by stable `content`,
`tool_call`, `tool_result`, `error`, and `done` events carrying camelCase
`turnId`, `messageId`, `round`, and `seq` fields.

Tenant and routing identity are supplied separately as a trusted
`ExecutionContext`; `AgentRunRequest` forbids user, Agent, Session, and root
execution identifiers, so model or client chat arguments cannot replace them.

The runner loads and normalizes the session, adds the new user turn, executes
allowed tools, and persists only after a final assistant response. Closing or
cancelling the stream closes the upstream provider stream. No partial assistant
or tool-call history is committed.

## History repair

The normalizer gives empty or duplicate tool-call IDs deterministic IDs,
removes orphan, late, and duplicate tool results, and inserts a structured
failure result for every unanswered tool call. Current Go message fields such
as `content_parts`, `_raw`, metadata, origin, provider, and model are accepted
and preserved for replay.

## Tool safety

Tools run behind a registry and explicit allow policy. The first built-ins are:

- `read_file`, confined to a configured workspace root with a size limit;
- `exec`, using an executable allow-list, argument arrays, and no shell; and
- `web_fetch`, restricted to HTTP(S) with a response size limit.

Tool exceptions, malformed arguments, denials, and timeouts are visible to both
the model and event consumer. Cancellation is never converted into a tool
failure and propagates to the caller.

## Acceptance evidence

The test suite pins one provider request per round, streaming event ordering,
trusted context delivery, raw assistant replay, final-only persistence,
upstream close on Stop, history repair, tool policy, timeout, exception, file
confinement, and HTTP scheme handling.
