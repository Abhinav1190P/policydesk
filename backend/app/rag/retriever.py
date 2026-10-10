import asyncio
import logging
import re
from dataclasses import asdict, dataclass

from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings
from app.db.pool import get_pool
from app.providers import bedrock
from app.providers.factory import get_embedder
from app.providers.text import terms

log = logging.getLogger("policydesk.retriever")
RRF_K = 60
_iterative_scan: bool | None = None


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    title: str
    heading: str | None
    content: str
    context: str | None
    score: float
    similarity: float
    source: str

    def citation(self, n: int) -> dict:
        d = asdict(self)
        d["n"] = n
        d["snippet"] = self.content[:280]
        d.pop("context", None)
        return d


async def _supports_iterative_scan(conn) -> bool:
    global _iterative_scan
    if _iterative_scan is None:
        v = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        major, minor = (int(x) for x in (v or "0.0").split(".")[:2])
        _iterative_scan = (major, minor) >= (0, 8)
    return _iterative_scan


def keyword_query(q: str) -> str:
    ts = [t for t in dict.fromkeys(terms(q)) if re.fullmatch(r"[a-z0-9]+", t)]
    return " | ".join(f"{t}:*" for t in ts[:12])


class PgRetriever:
    name = "pgvector"

    async def retrieve(self, tenant_id: str, query: str, k: int | None = None, mode: str = "hybrid", category: str | None = None, embedding: list[float] | None = None) -> tuple[list[Hit], int]:
        s = get_settings()
        k = k or s.retrieval_k
        tokens = 0
        if embedding is None:
            vecs, tokens = await get_embedder().embed([query], kind="query")
            embedding = vecs[0]
        pool = get_pool()
        fetch_n = k * 4
        cat_sql = " AND d.category = $4" if category else ""
        args_extra = [category] if category else []
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.ef_search = 100")
                if await _supports_iterative_scan(conn):
                    await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                vec_rows = await conn.fetch(
                    f"""
                    SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
                    WHERE c.tenant_id = $2 AND d.status = 'ready'{cat_sql}
                    ORDER BY c.embedding <=> $1 LIMIT $3
                    """,
                    embedding, tenant_id, fetch_n, *args_extra,
                )
            kw_rows = []
            tsq = keyword_query(query)
            if mode == "hybrid" and tsq:
                kw_rows = await conn.fetch(
                    f"""
                    SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id,
                         to_tsquery('english', $1) q
                    WHERE c.tenant_id = $2 AND d.status = 'ready' AND c.tsv @@ q{cat_sql}
                    ORDER BY ts_rank_cd(c.tsv, q) DESC LIMIT $3
                    """,
                    tsq, tenant_id, fetch_n, *args_extra,
                )
            fused: dict[int, float] = {}
            for rank, r in enumerate(vec_rows):
                fused[r["id"]] = fused.get(r["id"], 0.0) + 1.0 / (RRF_K + rank + 1)
            for rank, r in enumerate(kw_rows):
                fused[r["id"]] = fused.get(r["id"], 0.0) + 1.0 / (RRF_K + rank + 1)
            if not fused:
                return [], tokens
            top_ids = sorted(fused, key=lambda i: -fused[i])[: k * 2]
            rows = await conn.fetch(
                """
                SELECT c.id, c.document_id, d.title, c.heading, c.content, c.context,
                       1 - (c.embedding <=> $1) AS similarity
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE c.id = ANY($2::bigint[]) AND c.tenant_id = $3
                """,
                embedding, top_ids, tenant_id,
            )
        by_id = {r["id"]: r for r in rows}
        hits: list[Hit] = []
        seen_ctx: set[tuple[str, str]] = set()
        for cid in top_ids:
            r = by_id.get(cid)
            if r is None or r["similarity"] < s.min_relevance:
                continue
            if r["context"]:
                key = (str(r["document_id"]), r["context"])
                if key in seen_ctx:
                    continue
                seen_ctx.add(key)
            hits.append(
                Hit(str(r["id"]), str(r["document_id"]), r["title"], r["heading"], r["content"], r["context"], round(fused[cid], 6), round(float(r["similarity"]), 4), self.name)
            )
            if len(hits) >= k:
                break
        return hits, tokens


def build_kb_retrieve_request(kb_id: str, tenant_id: str, query: str, k: int, search_type: str = "DEFAULT", category: str | None = None) -> dict:
    tenant_filter = {"equals": {"key": "tenant_id", "value": tenant_id}}
    flt = {"andAll": [tenant_filter, {"equals": {"key": "category", "value": category}}]} if category else tenant_filter
    vector_cfg: dict = {"numberOfResults": k, "filter": flt}
    if search_type != "DEFAULT":
        vector_cfg["overrideSearchType"] = search_type
    return {
        "knowledgeBaseId": kb_id,
        "retrievalQuery": {"text": query},
        "retrievalConfiguration": {"vectorSearchConfiguration": vector_cfg},
    }


class BedrockKBRetriever:
    name = "bedrock_kb"

    async def retrieve(self, tenant_id: str, query: str, k: int | None = None, mode: str = "hybrid", category: str | None = None, embedding=None) -> tuple[list[Hit], int]:
        s = get_settings()
        k = k or s.retrieval_k
        req = build_kb_retrieve_request(s.bedrock_kb_id, tenant_id, query, k, s.bedrock_kb_search_type, category)
        try:
            resp = await asyncio.to_thread(bedrock.client("bedrock-agent-runtime").retrieve, **req)
        except (ClientError, BotoCoreError) as e:
            raise bedrock.to_provider_error(e)
        hits = []
        for i, r in enumerate(resp.get("retrievalResults", [])):
            meta = r.get("metadata") or {}
            if meta.get("tenant_id") not in (None, tenant_id):
                log.error("kb returned a chunk for another tenant; dropping it")
                continue
            uri = (r.get("location") or {}).get("s3Location", {}).get("uri", "")
            score = float(r.get("score") or 0.0)
            if score < s.min_relevance:
                continue
            hits.append(
                Hit(
                    meta.get("x-amz-bedrock-kb-chunk-id", f"kb-{i}"),
                    meta.get("doc_id", uri),
                    meta.get("title", uri.rsplit("/", 1)[-1]),
                    None,
                    r["content"]["text"],
                    None,
                    round(score, 6),
                    round(score, 4),
                    self.name,
                )
            )
        return hits, 0


def get_retriever():
    return BedrockKBRetriever() if get_settings().retriever == "bedrock_kb" else PgRetriever()
