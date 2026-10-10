# Eval results (stub LLM, stub embeddings, top-5)

| Chunking | Chunks | Retrieval hit@k | Answer accuracy | Refusal accuracy | Cross-tenant refused | Leaked chunks | Tool choice | p50 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| hierarchical | 50 | 100% | 95% | 100% | 3/4 | 0 | 100% | 3.0 |

## Misses

- [hierarchical] a09 (answerable): What is the minimum password length? -> I couldn't find this in your company's policy documents. Please contact HR or IT for help with this one.
- [hierarchical] x04 (cross_tenant): Which app is used for multi-factor authentication? -> Company email can be used on personal phones only with the Intune app installed. [1]
