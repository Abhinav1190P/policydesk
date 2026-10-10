# Eval results (groq LLM, cohere embeddings, top-5)

| Chunking | Chunks | Retrieval hit@k | Answer accuracy | Refusal accuracy | Cross-tenant refused | Leaked chunks | Tool choice | p50 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed | 50 | 100% | 95% | 100% | 4/4 | 0 | 50% | 842.0 |
| hierarchical | 50 | 100% | 95% | 100% | 4/4 | 0 | 100% | 437.0 |
| semantic | 71 | 100% | 95% | 100% | 4/4 | 0 | 50% | 876.0 |

## Misses

- [fixed] a15 (answerable): What is the shift differential for the night shift? -> The night shift (Shift C) earns a shift differential of **10 % of basic pay** for each night shift worked【1】.
- [fixed] t02 (action): My laptop battery is broken, please raise a ticket -> None
- [fixed] t03 (action): I am locked out of my account and cannot log in -> None
- [hierarchical] a09 (answerable): What is the minimum password length? -> I couldn't find this in your company's policy documents. Please contact HR or IT for help with this one.
- [semantic] a15 (answerable): What is the shift differential for the night shift? -> The night shift (Shift C) receives a shift differential of **10 % of basic pay**【1】.
- [semantic] t02 (action): My laptop battery is broken, please raise a ticket -> None
- [semantic] t03 (action): I am locked out of my account and cannot log in -> None
