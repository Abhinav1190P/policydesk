# usage_ledger benchmark (10,000,000 rows, 200 tenants, 12 months)

| Variant | Query | Median ms | Buffers (hit/read) | Scan nodes | Relations touched |
|---|---|---:|---:|---|---:|
| flat, no index | Q1 tenant monthly cost | 918.978 | 192/153655 | Seq Scan | 1 |
| flat, no index | Q2 tenant latest 50 rows | 605.07 | 384/153463 | Seq Scan | 1 |
| flat, no index | Q3 platform 1-day totals | 1356.355 | 576/153271 | Seq Scan | 1 |
| flat + btree(tenant_id, created_at) + brin(created_at) | Q1 tenant monthly cost | 5.698 | 4082/0 | Bitmap Heap Scan, Bitmap Index Scan | 1 |
| flat + btree(tenant_id, created_at) + brin(created_at) | Q2 tenant latest 50 rows | 0.035 | 48/0 | Index Scan | 1 |
| flat + btree(tenant_id, created_at) + brin(created_at) | Q3 platform 1-day totals | 10.534 | 475/0 | Bitmap Heap Scan, Bitmap Index Scan | 1 |
| flat + btree only | Q1 tenant monthly cost | 5.323 | 4082/0 | Bitmap Heap Scan, Bitmap Index Scan | 1 |
| flat + btree only | Q2 tenant latest 50 rows | 0.047 | 48/0 | Index Scan | 1 |
| flat + btree only | Q3 platform 1-day totals | 1187.278 | 4584/149263 | Seq Scan | 1 |
| monthly partitions + local btree | Q1 tenant monthly cost | 5.871 | 4136/0 | Bitmap Heap Scan, Bitmap Index Scan | 1 |
| monthly partitions + local btree | Q2 tenant latest 50 rows | 0.095 | 47/0 | Index Scan | 12 |
| monthly partitions + local btree | Q3 platform 1-day totals | 128.059 | 4274/8547 | Seq Scan | 1 |

| Object | Size MB |
|---|---:|
| flat_table | 1260.3 |
| flat_btree | 315.4 |
| flat_brin | 0.18 |
| partition_btrees_total | 357.4 |

Index build: btree 8.94 s, brin 1.67 s

Retention: DELETE one month on flat table 0.666 s (DELETE 833334), DETACH + DROP one partition 0.098 s
