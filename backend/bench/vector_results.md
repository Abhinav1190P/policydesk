# pgvector HNSW benchmark (100,000 vectors, 1024 dims, 200 queries, recall@10, 50 tenants)

Exact scan: p50 484.33 ms, p95 597.33 ms; with tenant filter p50 24.55 ms
HNSW (m=16, ef_construction=64) build 70.9 s, index 819.2 MB, table 561.7 MB

| ef_search | recall@k | p50 ms | p95 ms | planner's choice with tenant filter | filtered recall via HNSW, iterative off | avg rows returned | filtered recall via HNSW, iterative on | p95 ms (iterative on) |
|---:|---:|---:|---:|---|---:|---:|---:|---:|
| 10 | 0.7805 | 1.82 | 3.87 | exact | 0.021 | 0.21 | 0.924 | 17.85 |
| 20 | 0.8945 | 2.09 | 3.15 | exact | 0.0405 | 0.43 | 0.9255 | 18.67 |
| 40 | 0.9345 | 2.45 | 3.75 | exact | 0.0845 | 0.87 | 0.9295 | 18.89 |
| 80 | 0.971 | 2.74 | 4.18 | exact | 0.175 | 1.8 | 0.9355 | 19.18 |
| 160 | 0.9865 | 2.95 | 5.23 | exact | 0.333 | 3.37 | 0.9435 | 19.68 |
| 320 | 0.996 | 3.56 | 6.21 | exact | 0.644 | 6.45 | 0.9525 | 23.09 |
