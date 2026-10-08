# Team Submission

## Team

- Team name: hive
- Participants: Karan Sharma
- Company Brain / project name: Hive: self-healing services that learn from each other without sharing secrets

## Company Brain Overview

Every team re-debugs the same failures because the incident lives in their private logs, code and
Slack. Hive gives each service an on-call healer agent with a **private** Cognee brain (its logs, code,
runbook, incidents, secrets) and a shared **hive** of sanitized problem patterns. The healer observes the
service, remembers the failing logs, recalls its own memory plus hive patterns, patches config/code,
restarts and re-observes until healthy. Then it forms memories: the full incident privately, and a
sanitized pattern (symptom → root cause → fix shape; no project names, keys, ports or secrets) into the
hive. The next team that hits the same problem in a different vocabulary heals from the hive without
ever seeing the first team's raw memory.

- Data sources connected through Scalekit: Slack (`#oncall-<project>` runbooks) and GitHub (service code; fix PRs out)
- Primary workflow: on-call incident response / self-healing
- Users: `oncall-checkout@`, `oncall-billing@`, `hive@` curator; each on-call owns only its project brain and reads only the hive
- Stand-out: cross-team learning with secrecy enforced by Cognee permissions **and** a canary-token leak test in the eval

## The Three Layers

### Pull — Scalekit

- Connections: `slack` → Slack, `github-connect` → GitHub
- Tools: `slack_fetch_conversation_history`, `github_file_contents_get`; write-back: `github_branch_create`, `github_file_create_update`, `github_pull_request_create`, `slack_send_message`
- Identity: Scalekit `identifier` per on-call user ↔ Cognee user
- Write-back: `heal --ship` pushes the fix to a branch, opens a PR and posts to `#oncall-<project>` as the on-call user, behind a y/N human confirm
- Code: `healer.py` (`runbook`, `ship_fix`). Demo uses the recorded pull in `fixtures/` (`runbook -f`).

### Remember — Cognee

- Permanent graph: failing logs (`source:logs`), runbook (`source:slack`), code (`source:github`), incident write-ups (`source:incident`) per project; sanitized patterns (`source:hive`, `pattern:<id>`) in the hive
- Datasets: `checkout-brain` (oncall-checkout only), `billing-brain` (oncall-billing only), `hive` (curator writes; both on-calls granted read)
- Access control: `ENABLE_BACKEND_ACCESS_CONTROL=true`; `authorized_give_permission_on_datasets` grants hive read
- Secrecy: each project's config + startup logs contain a canary token; the sanitizer refuses any pattern containing a canary or project name; the eval checks no foreign canary ever appears in recalled context
- Retrieval: `recall(query_type=CHUNKS)` over own brain (+ hive when on), query = the failing error line
- Code: `healer.py` (`setup`, `remember`, `recall`, `contribute_to_hive`, `seed`)

### Act + Evaluate — agent + Respan

- Agent: healer debug loop (observe → remember → recall → patch → restart → re-observe, max 4 iterations); sanitizer agent for hive contributions
- LLM via Respan gateway: `gpt-5-mini` (healer + sanitizer), `text-embedding-3-large` (Cognee)
- Tracing: `Respan()` + `@workflow("self_heal")`, `customer_identifier` = on-call user, metadata project + hive on/off
- Scenario: billing hits `ERR_WIRE_4012` (opaque error; the valid codec is not in its logs or code, only in checkout's past incident, which billing can only reach through the hive). 2 runs hive off, 1 run hive on (time-boxed).
- Evaluator: deterministic Python check: service healthy (HTTP 200) after patch, iterations to heal, hive hits, canary leaks
- Code: `healer.py eval`

## Evaluation Evidence

### Baseline Run (hive off)

- Billing, `ERR_WIRE_4012`, hive off: **0/2 healed** (4 iterations each, all failed), 0 hive hits, 0 leaks
- Results: `evals/results/before.json`

### After Run (hive on)

- Billing, `ERR_WIRE_4012`, hive on: **1/1 healed** (iteration 4), 2 hive hits, **0 leaks**
- Results: `evals/results/after.json`
- Traces: Respan project `selfheal-hive`, workflow `self_heal`

### What changed

Billing was granted read on the hive, which holds checkout's sanitized pattern for the same failure.

## Run it

See `README.md`: `setup` → `runbook -f` → `seed` → `eval -l before -n` → `eval -l after`.
