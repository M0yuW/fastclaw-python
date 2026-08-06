# Phase 7: Gateway and current Web compatibility

This phase connects the imported Next.js application to the application-scoped
`AgentRuntimeManager`. It does not introduce a second execution path: Web chat,
SSE, and OpenAI-compatible chat all use the same manager, queue, Provider,
session persistence, and cancellation ownership.

## Implemented compatibility surface

- administrator user and cross-tenant Agent inventories;
- explicit read-only `actAs` propagation and server-side enforcement;
- Agent update/delete, raw config, system-file overrides, and confined workspace
  listing/upload/download;
- Session list, history, rename, and deletion using the complete
  `(user_id, agent_id, session_key)` identity;
- installed Skill inventory, per-Agent bindings, local binding, tool policy,
  queue status, and non-secret system configuration;
- arbitrary imported Agent IDs in the statically exported Next.js routes;
- structured `501 not_implemented` envelopes for channels, plugins, cron,
  remote Skill registries, and other post-cutover modules.

Imported files remain unchanged on disk. The Runtime loads those files as its
base layer and applies database system-file edits in memory. Deleting an Agent
removes its database ownership and execution registration but deliberately
retains its filesystem assets for audit and recovery.

## Credential boundary

Provider CRUD stores endpoints, API type, model catalogs, and scope only. It
rejects plaintext API keys and reports the corresponding
`FASTCLAW_PROVIDER_<NAME>_API_KEY` environment variable. `/api/test-provider`
may use a submitted key for one transient request, but never persists it. Bulk
configuration similarly rejects plaintext Provider, channel, tool, and Skill
credentials.

## Browser and provenance gates

Playwright starts a disposable SQLite Gateway and deterministic local Provider.
The test covers login, Agent discovery, role/Skill pages, SSE chat, history
reload, and browser Abort-to-Runtime Stop propagation. No real Provider or user
database is used.

The Web source remains attributed to Go commit `792417b`. Python-specific
changes are listed in `tests/fixtures/web-python-overlays.json`; CI verifies all
other upstream files byte-for-byte against the original manifest.
