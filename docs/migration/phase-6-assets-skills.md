# Phase 6: assets, Skills, and production tools

This phase makes imported production profiles executable without reading or
writing the Go runtime directory.

## Asset import

`fastclaw migrate import-assets` reads valid Agent IDs from the Python database
and considers only those Agent/workspace trees plus shared Skills. It excludes
stale Agent directories, caches, logs, virtual environments, VCS metadata,
debug artifacts, database files, environment files, private-key formats, and
the benchmark tenant artifact. Symbolic links are never followed.

The command completes source hashing and conflict preflight before its first
target write. Dry-run creates no target directory. Existing identical files are
reported as unchanged, divergent files abort the whole operation, and new files
are copied through a same-directory temporary file with a post-copy SHA-256
check. Interrupted imports are safe to repeat.

## Skill identity and preparation

Skills are indexed by the `name` field in `SKILL.md` frontmatter, not their
directory name. This intentionally resolves the `findata-toolkit` directory as
`findata-toolkit-us`.

`fastclaw skills prepare` is the only dependency-installation path. Each
environment path includes a hash of the Python minor version and
`requirements.txt`. A valid marker is written only after the environment and
dependencies are complete. Runtime execution never invokes pip; an unprepared
or stale environment fails closed and keeps `/readyz` false when the Skill is
required by a profile.

Only environment variables explicitly declared by Skill frontmatter are
injected. For the imported assets this means `ODDS_API_KEY` is visible only to
`match-data-toolkit`.

## Prompt and execution policy

- Profile prompts append every `skills.alwaysLoad` instruction document.
- References to the legacy data root are rewritten only in the in-memory prompt;
  imported files remain byte-identical for audit.
- The `exec` tool accepts only Python files under an allowed Skill's `scripts/`
  directory and its prepared interpreter. Shells, `-c`, `-m`, stdin programs,
  arbitrary executable paths, and scripts outside the selected Skill are
  rejected.
- stdout/stderr share one byte limit. Cancellation, timeout, or overflow
  terminates and reaps the entire process group.
- Workspace listing and writes remain confined to the current Agent directory;
  writes use fsync plus atomic replace.

## World Cup ledger

`worldcup_ledger` provides structured `append`, `settle`, and `report`
operations against the current Agent's Python workspace. Updates are serialized,
validated, and atomically replaced. Duplicate `(date, match)` entries are
rejected. Reports set `direct_return`, so the table becomes the final assistant
message without a second model request that could drop rows.

## Central configuration

`fastclaw providers check` lists required providers and Skills without printing
credential values. DeepSeek and OpenRouter use their official OpenAI-compatible
base URLs when a database endpoint is absent, so their named environment keys
are sufficient. Agent-less model defaults are inherited from the imported
`agents.defaults` setting, which covers LEO without per-Agent reconfiguration.

## Verification

- The real asset dry-run found no conflicts or pending copies for 27 valid
  Agents; stale aliases and memory logs were excluded.
- The three required environments (`findata-toolkit-cn`,
  `findata-toolkit-us`, and `match-data-toolkit`) were explicitly prepared, and
  their script help entry points completed under the isolated interpreters.
- Unit tests cover frontmatter/directory mismatch, prompt path mapping,
  requirements-hash invalidation, environment allowlisting, inline/module/path
  execution denial, asset dry-run/idempotency/conflicts, atomic workspace writes,
  ledger uniqueness/settlement/direct return, and system default inheritance.

Provider and ODDS credentials remain intentionally absent. Runtime readiness
must remain false until the operator supplies the three centralized values.
