# Hive: self-healing services that learn from each other without sharing secrets

Services break in the same ways across teams: a pool sized to zero, a timeout below upstream latency,
a lowercase currency code. Every team re-debugs it from scratch, because the incident lives in their
private logs, code and Slack, and nobody may read anyone else's.

**Hive** gives each service a healer agent with a private Cognee brain, and a shared hive of abstract
problem patterns. The healer:

1. **observes** the service (starts it, probes it, reads its logs),
2. **remembers** the failing logs into its project's private brain,
3. **recalls** its own past incidents, its runbook (Slack `#oncall-<project>`) and code (GitHub), both
   pulled through Scalekit as that team's on-call, plus **hive patterns** from other teams,
4. **patches** config or code, restarts, re-observes, until healthy,
5. **forms a memory**: the full incident into its private brain, and a sanitized pattern (symptom class →
   root cause → fix shape, no names, keys, values or secrets) into the hive,
6. optionally **ships** the fix as a branch + PR and a Slack post through Scalekit, as that on-call user,
   behind a y/N confirm.

Each hive pattern is tagged `pattern:<id>` and linked to every project that hit it, so the hive is a
hypergraph of similar problems across projects, while each project's raw memory stays its own.

Built at Cognee × Scalekit × Respan, SF Tech Week, 2026-10-07.

## Access

| Identity | Scalekit | Cognee |
|---|---|---|
| `oncall-checkout@` | slack `#oncall-checkout`, github | owns `checkout-brain`; reads `hive` |
| `oncall-billing@` | slack `#oncall-billing`, github | owns `billing-brain`; reads `hive` |
| `hive@` (curator) | — | owns `hive`; only sanitized patterns are written to it |

`ENABLE_BACKEND_ACCESS_CONTROL=true`: billing cannot read `checkout-brain` (and vice versa); it only
reaches checkout's lesson through the hive. Each project's config holds a **canary token**
(`internal_api_token`) that also appears in its logs. The sanitizer refuses to write a pattern that
contains any canary or project name, and the eval checks that no canary ever shows up in another
project's recalled context.

The two services fail the same way in different vocabulary (`db_pool_size` vs `pg_connections`,
`currency` vs `ccy`, `upstream_timeout_ms` vs `ledger_timeout_ms`), so a hive hit is real transfer,
not string matching.

## Run it

```bash
uv venv -p 3.12 && uv pip install "cognee>=1.6.3" scalekit-sdk-python python-dotenv openai respan-ai
cp .env.example .env              # Respan key, Scalekit creds, GITHUB_OWNER/GITHUB_REPO for --ship
python healer.py setup            # users, private brains, hive + read grants
python healer.py runbook -f       # runbook + code into each private brain (drop -f to pull via Scalekit)
python healer.py break billing pool && python healer.py heal billing
python healer.py eval -l before -n    # hive off
python healer.py eval -l after        # hive on
```

`cognee-cli -ui` shows the graph: pattern nodes in the hive, project nodes in each private brain.

## Eval

`healer.py eval` injects each fault into checkout first, then into billing, and heals each one. All
LLM calls go through the Respan gateway; each heal is a `self_heal` workflow trace tagged with the
on-call user, project and hive on/off.

| Metric | Meaning |
|---|---|
| `heal_rate` | fraction of faults healed within 4 iterations |
| `billing_mean_iters` | iterations billing needs to heal faults checkout already saw |
| `billing_hive_hits` | hive patterns in billing's recalled context |
| `leaks` | another project's canary in recalled context (must be 0) |

| Run | Change | heal_rate | billing_mean_iters | leaks |
|---|---|---|---|---|
| before | hive off | 0/2 | 4 (all failed) | 0 |
| after | hive on | 1/1 | 4 | 0 |
