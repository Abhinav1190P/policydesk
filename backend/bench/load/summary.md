| Scenario | API instances | Requests | Failures | Throughput (req/s) | p50 ms | p95 ms | p99 ms | Split across instances |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| cache miss (retrieve + generate + write) | 1 | 2832 | 0 | 47.9 | 35 | 96 | 2800 | {'api-1': 3241} |
| cache miss (retrieve + generate + write) | 3 | 2965 | 0 | 50.2 | 36 | 85 | 1300 | {'api-1': 1121, 'api-2': 1137, 'api-3': 1124} |
| cache hit (Redis) | 1 | 2977 | 0 | 50.4 | 19 | 41 | 2300 | {'api-1': 3373} |
| cache hit (Redis) | 3 | 3077 | 0 | 52.1 | 18 | 35 | 480 | {'api-1': 1155, 'api-2': 1170, 'api-3': 1175} |
| cache miss, 400 ms simulated LLM, 150 users | 1 | 3727 | 0 | 63.0 | 810 | 1500 | 7600 | {'api-1': 4329} |
| cache miss, 400 ms simulated LLM, 150 users | 3 | 4687 | 0 | 78.7 | 560 | 1200 | 3800 | {'api-1': 1783, 'api-2': 1768, 'api-3': 1808} |
