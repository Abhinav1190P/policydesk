# PolicyDesk

A multi-tenant HR and IT policy assistant. Employees ask questions and get answers drawn only from their own company's documents, with numbered citations. An agent can also look up their leave balance and raise IT tickets, and it asks the employee to confirm before it creates anything.

The AI layer can be swapped through one setting, so the same code runs fully offline, for free on Groq, or on Amazon Bedrock:

| Setting | `stub` | `local` | `groq` | `bedrock` |
|---|---|---|---|---|
| LLM | extractive, deterministic | Ollama (OpenAI-compatible API) | Groq `openai/gpt-oss-20b` | Bedrock Converse / ConverseStream (Nova, Claude, Llama) |
| Embeddings (`EMBED_BACKEND`) | 1024-dim feature hashing | Ollama `mxbai-embed-large` | Cohere `embed-english-v3.0` | Titan Text Embeddings v2 |
| Retriever (`RETRIEVER`) | pgvector hybrid | pgvector hybrid | pgvector hybrid | Bedrock Knowledge Base on S3 Vectors |
| Agent (`AGENT_MODE`) | local tool loop | local tool loop | local tool loop | Bedrock Agent with action groups |
| Cost | free | free | free tier | pay per token |

## Architecture

```
React (Vite) ──► nginx (least_conn) ──► FastAPI x3 ──► Postgres + pgvector  (tenants, chunks, chats, tickets, usage_ledger partitioned by month)
                                          │   │
                                          │   └──► Redis  (answer cache, token-bucket rate limits, agent sessions, daily AI budget)
                                          └──► RabbitMQ ──► workers xN  (ingest queue: extract → chunk → embed / KB sync,
                                                                          usage queue: idempotent ledger writes,
                                                                          per-attempt retry queues + dead-letter queues)
Bedrock mode:  FastAPI ──► Converse (+ Guardrail with contextual grounding) · Retrieve (tenant metadata filter) · InvokeAgent
               Agent ──► Lambda (get_leave_balance → HMAC-signed call to /internal/tools)   ·   RETURN_CONTROL (create_it_ticket → user confirms in UI)
               S3 docs bucket + .metadata.json sidecars ──► Knowledge Base (Titan v2, S3 Vectors) ──► CloudWatch invocation logs
```

### How a question is answered (`app/services/chat.py`)
1. Per-user and per-tenant token buckets in Redis (Lua script, atomic). Over the limit returns 429 with `Retry-After`.
2. Daily token budget per tenant in Redis. When it runs out, the request moves to `FALLBACK_BACKEND` (for example Bedrock → Groq), or returns 429 if no fallback is set.
3. First-turn questions check the answer cache. The key is `tenant + doc_version + backend + normalised question`, and uploading or deleting a document bumps `doc_version`, so stale answers are never served.
4. Retrieval is always scoped to the tenant. With pgvector it runs HNSW cosine search and Postgres full-text search, then merges them with reciprocal rank fusion. `hnsw.iterative_scan` is enabled so the tenant filter doesn't starve results (see the benchmark). With a Bedrock KB it uses `Retrieve` with an `equals tenant_id` metadata filter, and any chunk tagged with another tenant is dropped as a second line of defence.
5. If nothing clears `MIN_RELEVANCE`, the app returns a fixed "not in your policies" reply and skips the LLM call entirely.
6. The answer streams over SSE. On Bedrock with a guardrail, the context and question are sent as `guardContent` blocks qualified `grounding_source` and `query`, so contextual grounding checks the answer against the retrieved text. Other backends get regex PII masking that leaves contacts quoted in the policy unmasked.
7. Retries use exponential backoff with jitter: botocore adaptive retry mode for Bedrock, and a custom retry loop for HTTP backends. If the primary backend still fails before any token has streamed, the fallback backend takes over.
8. Usage events (tokens, list-price cost, latency, cached flag) go to RabbitMQ. A worker writes them idempotently (`ON CONFLICT DO NOTHING` on `event_id`) into the monthly-partitioned `usage_ledger`. If RabbitMQ is down, the API writes directly.

### Agent (`app/agent`)
- **Tools:** `search_policies`, `get_leave_balance`, `create_it_ticket`. The employee's identity always comes from the JWT or session state and never from model arguments, so the model can't ask for someone else's leave.
- **Confirmation:** `create_it_ticket` needs the employee to confirm. The pending action is stored in Redis under `agent:{user}:{session}`, so another user can't confirm it. Arguments are re-validated server-side, and the ticket insert uses the action id as an idempotency key, so a double-click creates one ticket.
- **Bedrock mode:** `InvokeAgent` passes `sessionAttributes` (tenant_id, user_id) and a per-request `knowledgeBaseConfigurations` filter on tenant_id. `get_leave_balance` runs in a Lambda that reads identity from `sessionAttributes` and calls the app with an HMAC-SHA256 signature and timestamp (300 s skew window). `create_it_ticket` uses `RETURN_CONTROL`: the app shows the confirm card, creates the ticket, then resumes the agent with `returnControlInvocationResults`.

### Ingestion (`app/services/ingest.py`, `app/worker.py`)
Uploads are stored, deduplicated by SHA-256 per tenant, and published to the `ingest` queue. Workers use manual acks and a prefetch limit. Errors are handled in three ways:
- **Permanent errors** (unreadable PDF, no text): the document is marked failed and the message is rejected to `ingest.dlq`.
- **Transient errors** (throttling, timeouts, a KB ingestion job already running): the message is republished to `ingest.retry.1` (2 s TTL), then `ingest.retry.2` (4 s), which dead-letter back to `ingest`. After 3 attempts it goes to the DLQ and the document is marked failed. Admins can retry from the UI.

There are three chunking strategies, matching the Bedrock KB options: `fixed` (200 words, 40 overlap), `hierarchical` (600-word parents with 120-word children; the child is embedded and the parent is sent to the LLM), and `semantic` (breaks where the embedding distance between sentences crosses a percentile threshold).

## Measured results

All numbers come from this repo's scripts on a 2-vCPU, 7 GB cloud sandbox running Postgres 16, Redis, RabbitMQ, nginx and Locust together, with stock Postgres settings (`shared_buffers=128MB`).

### PostgreSQL: usage ledger indexing and partitioning (`scripts/bench_usage.py`, 10M rows, 200 tenants, 12 months)

| Query | No index | btree (tenant_id, created_at) | + BRIN (created_at) | Monthly partitions + local btree |
|---|---:|---:|---:|---:|
| Tenant's cost for one month | 919 ms | **5.3 ms** | 5.7 ms | 5.9 ms |
| Tenant's latest 50 rows | 605 ms | **0.047 ms** | 0.035 ms | 0.095 ms |
| Platform-wide totals for one day | 1,356 ms | 1,187 ms (btree can't help) | **10.5 ms** | 128 ms (pruned to one partition) |
| Drop one month of data | `DELETE` 666 ms | | | **`DETACH` + `DROP` 98 ms** |

Index sizes on the 1.26 GB table: the btree is 315 MB and the BRIN is **0.18 MB**. The takeaways:
- The composite btree serves tenant-scoped queries, about 170x faster than no index.
- BRIN serves time-range scans across all tenants on append-only data, at 1/1700th of the btree's size.
- Partitioning mainly buys cheap retention and partition pruning, rather than faster point lookups.

The app's migrations use the same design: a composite btree, a BRIN index, and monthly partitions created ahead of time by the worker. Full output is in `backend/bench/usage_results.md`.

### pgvector: HNSW recall vs latency, and multi-tenant filtering (`scripts/bench_vector.py`, 100k × 1024-dim, 50 tenants, 200 queries)

| ef_search | recall@10 | p50 ms | p95 ms | Tenant-filtered recall, iterative scan off | Rows returned | Tenant-filtered recall, `iterative_scan = relaxed_order` |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 0.78 | 1.8 | 3.9 | 0.02 | 0.2 | 0.92 |
| 40 | 0.93 | 2.5 | 3.8 | 0.08 | 0.9 | 0.93 |
| 80 | 0.97 | 2.7 | 4.2 | 0.18 | 1.8 | 0.94 |
| 320 | 1.00 | 3.6 | 6.2 | 0.64 | 6.5 | 0.95 |

An exact scan takes 484 ms at p50. HNSW takes 2–4 ms with a 71 s build and an 819 MB index. When one tenant holds 2% of the rows and the planner uses HNSW, post-filtering returns fewer than one row on average at the default `ef_search=40`. Iterative scan (pgvector 0.8) restores recall to about 93%, which is why the retriever turns it on. In practice the planner chooses an exact scan (24 ms) for a filter this selective, which is also correct.

### Load tests (`scripts/run_loadtests.sh`, Locust, stub backend, nginx least_conn)

| Scenario | Instances | Throughput | p50 | p95 | p99 | Errors |
|---|---:|---:|---:|---:|---:|---:|
| Cache miss: retrieve + generate + DB writes, 60 users | 1 | 47.9 req/s | 35 ms | 96 ms | 2,800 ms | 0 |
| Cache miss, 60 users | 3 | 50.2 req/s | 36 ms | 85 ms | 1,300 ms | 0 |
| Cache hit (Redis), 60 users | 1 | 50.4 req/s | 19 ms | 41 ms | 2,300 ms | 0 |
| Cache hit, 60 users | 3 | 52.1 req/s | 18 ms | 35 ms | 480 ms | 0 |
| Cache miss, 400 ms simulated LLM, 150 users | 1 | 63.0 req/s | 810 ms | 1,500 ms | 7,600 ms | 0 |
| Cache miss, 400 ms simulated LLM, 150 users | 3 | **78.7 req/s** | **560 ms** | **1,200 ms** | **3,800 ms** | 0 |

How to read these:
- At 60 users the box isn't saturated, so throughput simply equals the offered load. The cache halves median latency (35 → 19 ms).
- The p99 tail comes from the start-up burst of bcrypt logins. Three instances cut that tail from 2.8 s to 1.3 s on cache misses, and from 2.3 s to 0.48 s on hits.
- Under heavier load (150 users), three instances served 25% more requests with 31% lower p50.
- nginx spread the requests evenly: 1,783 / 1,768 / 1,808.
- All of this ran on a 2-vCPU machine that also hosted the database, the broker and the load generator. More cores would show larger gains, so rerun on your own machine for resume numbers.

### Retrieval and answer eval (`eval/run_eval.py`, 32 questions)

Question types: 20 answerable, 4 unanswerable, 4 cross-tenant (the fact exists only in the other company's documents) and 4 agent tool-choice. Each run re-ingests every document with fixed, hierarchical and semantic chunking.

Stub baseline (lexical hashing embeddings plus an extractive "LLM"):

| Chunking | Hit@5 | Answer accuracy | Refusal accuracy | Cross-tenant refused | Leaked chunks | Tool choice |
|---|---:|---:|---:|---:|---:|---:|
| fixed | 100% | 95% | 100% | 3/4 | **0** | 100% |
| hierarchical | 100% | 95% | 100% | 3/4 | **0** | 100% |
| semantic | 100% | 95% | 100% | 3/4 | **0** | 100% |

The one cross-tenant miss is an over-eager answer from the asker's own company's documents, not a leak. Run the eval with `AI_BACKEND=groq EMBED_BACKEND=cohere` or the Bedrock settings to get real-model numbers; the eval writes `eval/results-<backend>-<embed>.md`.

## Run it locally

Requirements: Python 3.12+, Node 20+, Docker (or local Postgres 16 with pgvector 0.8, Redis and RabbitMQ).

```bash
cd infra && docker compose up -d postgres redis rabbitmq
cd ../backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python scripts/seed.py
uvicorn app.main:app --reload --port 8000
python -m app.worker
cd ../frontend && npm install && npm run dev
```

Run the uvicorn command and the worker in separate terminals. Then open http://localhost:5173 and sign in with a demo account (password `Password123!`):
- `employee@acme.example`
- `admin@acme.example`
- `employee@globex.example`
- `admin@globex.example`

Full stack with 3 API instances, 2 workers and nginx: `cd infra && docker compose up --build`, then open http://localhost:8080 and run `docker compose exec api-1 python scripts/seed.py`.

To use Ollama instead of the stub: `ollama pull llama3.1:8b && ollama pull mxbai-embed-large`, set `AI_BACKEND=local EMBED_BACKEND=local MIN_RELEVANCE=0.3`, then re-run the seed against a fresh database. Embeddings from different backends can't be mixed.

Tests need Postgres, Redis and RabbitMQ running, plus a `policydesk_test` database and a `policydesk_test` RabbitMQ vhost:

```bash
pytest -q
```

That runs 99 tests. They cover:
- tenant isolation, cache invalidation, SSE ordering and fallback
- budget and rate limits
- agent confirm, cancel, idempotency and cross-user protection
- retry → DLQ flows on a real broker
- HMAC and the Lambda
- Bedrock request shapes via botocore Stubber
- AWS setup requests validated against the AWS API models

Benchmarks: `python scripts/bench_usage.py`, `python scripts/bench_vector.py`, `bash scripts/run_loadtests.sh`.

## Deploy for free

| Piece | Service |
|---|---|
| API with the worker in-process (`RUN_WORKER_IN_PROCESS=true`) | Render free web service (`render.yaml`) |
| Frontend | Render static site |
| Postgres + pgvector | Neon free |
| Redis | Redis Cloud free (30 MB) |
| RabbitMQ | CloudAMQP free (Little Lemur) |
| LLM | Groq free tier, with `FALLBACK_BACKEND=stub` |
| Embeddings | Cohere trial key, or `stub` |

1. Create the Neon, Redis Cloud and CloudAMQP instances and copy their connection URLs.
2. Seed once from your machine with the deployed settings: `DATABASE_URL=<neon> REDIS_URL=<redis> EMBED_BACKEND=cohere COHERE_API_KEY=... python scripts/seed.py`.
3. In Render, create a Blueprint from the repo. Fill in `DATABASE_URL`, `REDIS_URL`, `RABBITMQ_URL`, `GROQ_API_KEY`, `COHERE_API_KEY`, `CORS_ORIGINS` (the static site URL) and `VITE_API_URL` (the API URL).

The free web service sleeps after 15 minutes idle, and the first request after that takes about 30–50 s.

## Connect AWS (Bedrock)

Everything runs from your machine with your own AWS credentials (`aws configure`). Keys never go into the app repo or chat.

```bash
pip install boto3 opensearch-py
export INTERNAL_TOOLS_SECRET=<same value as the app>
python aws/policydesk_aws.py setup --app-url https://<your-api>.onrender.com --budget-email you@example.com
```

Setup creates:
- a private, encrypted S3 docs bucket
- an S3 Vectors bucket and index (1024-dim cosine)
- a Knowledge Base with Titan v2 embeddings and a data source with the chosen chunking strategy
- a guardrail with content filters, prompt-attack filter, denied topic, PII/regex and contextual grounding, plus a version
- the leave-balance Lambda
- a Bedrock Agent with two action groups (Lambda and RETURN_CONTROL) and the KB association, plus a `live` alias
- model invocation logging to CloudWatch with 7-day retention
- a $5/month budget alert
- least-privilege IAM roles, and a `policydesk-app` IAM user whose policy is limited to these resources (create its access key yourself in the console)

It prints the `.env` lines to add.

Other commands:
- `python aws/policydesk_aws.py rechunk --chunking semantic` re-ingests with another chunking strategy for the eval.
- `python aws/hybrid_experiment.py run` creates a temporary OpenSearch Serverless collection (redundancy disabled), a second KB, and compares semantic and hybrid retrieval with tenant filters. It writes `aws/hybrid_results.md` and always deletes the collection when it finishes, even on errors. Expect well under $1 for a run.
- `python aws/policydesk_aws.py teardown` removes everything except the budget alert.

Cost notes:
- Bedrock has no free tier; Nova Lite is about $0.06 / $0.24 per million input / output tokens.
- Titan v2 embeddings are $0.02 per million tokens.
- S3 Vectors is pay-per-use with no idle floor. OpenSearch Serverless has an idle floor, which is why it is only used in the timed experiment.
- The daily token budget with Groq fallback, the Redis cache and the "skip the LLM when nothing is relevant" rule all keep spend down.

## Spending protection

Several guards stop a bug or a burst of traffic from draining AWS credits:

- **Daily AWS cap (`AWS_DAILY_USD_LIMIT`, default $0.50).** Every Bedrock and Bedrock Agent call adds its list-price cost to a Redis counter. Once the cap is hit, chat and the agent move to `FALLBACK_BACKEND` (Groq), and document ingestion that needs AWS fails without retrying.
- **Circuit breaker.** Five Bedrock failures within 2 minutes pause Bedrock for 10 minutes, so a broken config or throttling doesn't keep firing paid calls.
- **Retry limits.** Retries are capped (`PROVIDER_MAX_RETRIES=2`, using botocore adaptive mode). Ingestion runs at most 3 attempts, and non-retryable errors such as AccessDenied, validation errors or a paused backend go straight to the dead-letter queue.
- **Size limits.** `MAX_TOKENS=700` caps the output, `MAX_CONTEXT_CHARS=8000` caps the context, questions are capped at about 500 tokens, and the agent stops after 5 steps.
- **Other cost controls.** Per-tenant daily token budgets, per-user rate limits, the Redis answer cache, and skipping the LLM entirely when nothing relevant is retrieved.
- **Kill switch.** `AWS_ENABLED=false` stops every AWS call at once.
- **Backstop.** The AWS Budgets alert ($5/month, at 50% and 90%) is created by `aws/policydesk_aws.py setup`.

The admin Usage page shows today's AWS spend against the cap.

## Project layout

```
backend/app/        FastAPI app: config, db (pool, migrations), infra (redis, rate limit, cache, broker), providers (stub, OpenAI-compatible, Bedrock),
                    rag (chunking, retriever, prompts), agent (local runner, Bedrock agent, tools, state), services, routes, worker
backend/tests/      99 tests against real Postgres, Redis and RabbitMQ
backend/scripts/    seed, benchmarks, local cluster and load-test runners
backend/eval/       eval questions and harness
backend/bench/      measured results
backend/sample_docs two tenants' policy documents
aws/                setup / teardown / rechunk, hybrid experiment, Lambda
frontend/           React app: chat with streaming and citations, assistant with confirm cards, tickets, documents, usage dashboard
infra/              docker-compose (3 API, 2 workers, nginx) and nginx.conf
```
