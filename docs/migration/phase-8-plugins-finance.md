# Phase 8: JSON-RPC plugins and finance-tools

## Scope

This phase adds an application-owned JSON-RPC plugin lifecycle and ports the
`finance-tools` source snapshot from `M0yuW/fastclaw@351ee42`. The snapshot has
its own source record and MIT license under `plugins/finance-tools/`; no Go Git
history, state database, cache, credential, or experimental artifact is copied.

## Runtime contract

- Plugins are discovered only from configured roots with non-symlinked
  `plugin.json` manifests and trusted script entry points.
- Communication is line-delimited JSON-RPC 2.0 with a 1 MiB message limit,
  bounded calls, structured protocol errors, and process-group termination.
- A crash or timeout is isolated from the Gateway. The process is restarted for
  later calls, but an interrupted mutation is never replayed automatically.
- `userId`, `agentId`, `sessionId`, `rootExecutionId`, and `callPath` come only
  from `ExecutionContext`. Model arguments using these names are rejected.
- Model-visible failures contain stable categories. Filesystem paths and
  subprocess details remain in service logs rather than tool output.

The bundled finance plugin uses a Runtime-generated configuration. Skill roots,
requirements-hash Python interpreters, and its state database cannot be changed
through the Web API. The admin API may enable/disable the plugin, restart it,
or set a 1–300 second timeout. Only `ODDS_API_KEY` and environment variables
declared by the installed finance skills are passed to the plugin process.

## Finance compatibility

The source plugin exposes typed market-data, screen, macro, portfolio,
Serenity, thesis, watchlist, event fingerprint, and alert tools. Its migrated
layout is:

- `skills/findata-toolkit` for `findata-toolkit-us`;
- `skills/findata-toolkit-cn` for `findata-toolkit-cn`;
- `skills/serenity-skill` for the pinned research methodology;
- `data/finance-tools.db` for new Python-only tenant state.

The finance state database is never read from or written to the Go data root.
Theses, watchlists, and alerts are keyed by trusted user identity. Updates use
an explicit expected version and return a visible `version_conflict` instead of
silently overwriting concurrent work.

## Validation

Run the source contract, Runtime protocol tests, and tenant integration test:

```bash
cd plugins/finance-tools
../../.venv/bin/python -m unittest -v test_plugin.py
cd ../..
.venv/bin/pytest -q tests/test_plugins.py tests/test_finance_plugin.py
```

The tests cover discovery, handshake, tool listing, trusted context injection,
identity forgery rejection, timeout termination, crash restart without replay,
tenant isolation, optimistic version conflicts, watchlist/event deduplication,
and clean shutdown.

## Residual boundaries

Market-data calls still require their prepared Skill environments and external
service credentials. Provider and ODDS secrets are not copied from Go. A real
finance market-data smoke therefore remains not-ready until operators configure
the centralized environment variables; tenant-state tools can be verified
without network access.
