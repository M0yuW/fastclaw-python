# Phase 5: application Agent runtime

This phase replaces the Gateway's per-request `AgentRunner` and empty tool
registry with one application-scoped `AgentRuntimeManager`.

## Runtime contract

- FastAPI starts the database, base Runtime, and Agent manager in that order and
  stops them in reverse order.
- Root runs and delegated runs use the same bounded `AsyncTaskQueue`. Root runs
  acquire a global slot; nested runs inherit the root slot, so
  `max_concurrent=1` remains live.
- Profiles merge `agent.json` with explicit database configuration and load
  `SOUL.md`, `IDENTITY.md`, `USER.md`, and `MEMORY.md` from imported Agent files.
- `no-tools` produces an empty tool contract, `delegate-only` exposes only
  `spawn_subagent`, and `allowedTools` is an explicit custom allowlist. Legacy
  profiles without a policy receive only the Runtime's registered safe tools.
- Delegation preserves the trusted user, root execution, call path, and session
  key. Session persistence remains isolated by `(user_id, agent_id, key)`.
- Closing a root stream cancels its queue job and every nested job. Provider
  streams are closed before dynamically created providers are stopped.

## Provider credentials

Imported provider records remain non-secret. A provider named `deepseek` or
`openrouter` can obtain its key from
`FASTCLAW_PROVIDER_DEEPSEEK_API_KEY` or
`FASTCLAW_PROVIDER_OPENROUTER_API_KEY`, respectively. The database supplies the
non-secret endpoint and API type. The older generic environment settings remain
available as a compatibility fallback.

## Readiness

`/readyz` now reports database, Agent manager, provider, and skill checks. An
empty base Runtime no longer passes readiness through `all([])`: at least one
shared ready provider or a complete configured provider selection is required.
Skill-reference validation is completed in the following asset/skill phase.

## Verification

- A mock coordinator delegates to a no-tool specialist with global concurrency
  set to one and completes without deadlock.
- Coordinator and specialist use the same session key but persist to distinct
  Agent session rows.
- Closing a streaming root run closes the provider stream and does not persist a
  partial assistant message.
- Named environment credentials resolve without copying a secret into the
  imported provider row.
- Direct-return tool results finish and persist without a second model request.
- Existing queue tests retain cross-tenant, cycle, backpressure, cancellation,
  deduplication, and shutdown coverage.

## Known residuals

The default tool factory in this phase exposes only read-only workspace access,
public HTTP fetch, and same-tenant delegation. Asset import, skill discovery and
preparation, command-level Python execution policy, writable workspace tools,
and the World Cup ledger are owned by the next phase.
