# Football source policy migration

## Runtime contract

`football_data(action=evidence)` now accepts only competition, country, season, date, both
teams, and constrained odds options. Provider URLs, credentials, ESPN slugs, TheSportsDB
league IDs, and Odds API sport keys are rejected when supplied by a caller.

TheSportsDB resolves one reviewed competition and one fixture identity before supplemental
sources run. The returned fixture is never overwritten by ESPN or odds data. ESPN identity
mismatches are `rejected`; transport, readiness, and timeout failures are `unavailable`.
The Odds API is confirmed against its live catalog and falls back to the current Sporttery
feed. Supplemental failures produce partial success. If the primary fixture cannot be
confirmed, the Tool fails closed with `football_primary_unavailable` and instructs callers
not to infer facts.

Existing direct schedule/results/standings calls now take `competition` and optional `country`;
model-supplied `league_id` is no longer accepted. World Cup compatibility scripts remain
bundled, but production agents should use the evidence action.

## Finding disposition

| Finding | Disposition |
|---|---|
| TheSportsDB league ID selected by the model | Fixed: server resolves and validates one ID from the reviewed competition identity. |
| ESPN failure discarded valid primary data | Fixed: all supplemental failures degrade with sanitized source status. |
| ESPN could attach the wrong event | Fixed: slug, teams, event ID, and date are checked against the primary fixture. |
| curl missing or cancelled | Fixed: structured readiness plus kill-and-reap behavior; runtime image installs curl. |
| Odds script fixed to World Cup | Fixed: `--competition`, shared reviewed mapping, live catalog confirmation. |
| Odds key or arbitrary sport key accepted from model | Fixed: key is environment-only and no sport-key argument exists. |
| Odds failure blocked analysis | Fixed: Sporttery fallback, then explicit odds unavailable while primary evidence survives. |
| Runtime and Skill mappings could drift | Fixed: one Skill catalog and an exact Runtime/Skill parity test. |

No database migration is required. Deploy the wheel and restart the Gateway so the new Tool
schema and bundled Skill replace the previous versions.
