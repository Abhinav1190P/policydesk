import asyncio
import hashlib
import json
import logging
import uuid

from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings
from app.db.pool import get_pool
from app.infra import cache, spend
from app.infra.broker import INGEST, broker
from app.providers import bedrock
from app.providers.base import ProviderError
from app.providers.factory import get_embedder
from app.rag.chunking import chunk_text
from app.rag.extract import extract_text, kind_for
from app.services import usage

log = logging.getLogger("policydesk.ingest")
EMBED_BATCH = 32


class PermanentError(Exception):
    pass


class UploadError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def create_document(tenant_id: str, user_id: str | None, filename: str, mime: str | None, data: bytes, title: str | None = None, category: str = "general", publish: bool = True) -> dict:
    s = get_settings()
    kind = kind_for(filename, mime)
    if kind is None:
        raise UploadError("Only PDF, Markdown and text files are supported")
    if not data:
        raise UploadError("File is empty")
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise UploadError(f"File is larger than {s.max_upload_mb} MB", 413)
    if not title and kind == "md":
        first = data[:300].decode("utf-8", errors="ignore").lstrip().splitlines()
        if first and first[0].startswith("#"):
            title = first[0].lstrip("#").strip()
    sha = hashlib.sha256(data).hexdigest()
    pool = get_pool()
    existing = await pool.fetchrow("SELECT id, status FROM documents WHERE tenant_id = $1 AND sha256 = $2", uuid.UUID(tenant_id), sha)
    if existing:
        raise UploadError("This file was already uploaded", 409)
    row = await pool.fetchrow(
        """
        INSERT INTO documents (tenant_id, title, filename, mime, category, content, size_bytes, sha256, chunk_strategy, uploaded_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id, title, filename, category, status, chunk_strategy, size_bytes, created_at
        """,
        uuid.UUID(tenant_id), (title or filename.rsplit(".", 1)[0]).strip()[:200], filename[:255], kind,
        category.strip().lower()[:40] or "general", data, len(data), sha,
        s.chunk_strategy, uuid.UUID(user_id) if user_id else None,
    )
    doc = dict(row)
    doc["id"] = str(doc["id"])
    if publish:
        try:
            await broker.publish(INGEST, {"op": "ingest", "document_id": doc["id"], "tenant_id": tenant_id}, message_id=f"ingest-{doc['id']}")
        except Exception as e:
            await pool.execute("UPDATE documents SET status = 'failed', error = $2 WHERE id = $1", uuid.UUID(doc["id"]), "queue unavailable")
            raise UploadError(f"Ingestion queue unavailable: {e}", 503)
    return doc


async def claim(document_id: str) -> dict | None:
    return await get_pool().fetchrow(
        """
        UPDATE documents SET status = 'processing', attempts = attempts + 1, updated_at = now(), error = NULL
        WHERE id = $1 AND status <> 'ready'
        RETURNING id, tenant_id, title, filename, mime, category, content, chunk_strategy, attempts
        """,
        uuid.UUID(document_id),
    )


async def mark_failed(document_id: str, error: str) -> None:
    await get_pool().execute(
        "UPDATE documents SET status = 'failed', error = $2, updated_at = now() WHERE id = $1",
        uuid.UUID(document_id), error[:500],
    )


async def mark_queued(document_id: str, error: str) -> None:
    await get_pool().execute(
        "UPDATE documents SET status = 'queued', error = $2, updated_at = now() WHERE id = $1 AND status = 'processing'",
        uuid.UUID(document_id), f"retrying: {error[:400]}",
    )


async def process_document(document_id: str) -> str:
    s = get_settings()
    if s.retriever == "bedrock_kb" or s.embed_backend == "bedrock":
        reason = await spend.blocked_reason("bedrock")
        if reason:
            await mark_failed(document_id, f"AWS paused ({reason}); retry from the Documents page later")
            raise PermanentError(f"AWS paused: {reason}")
    doc = await claim(document_id)
    if doc is None:
        return "skipped"
    if get_settings().retriever == "bedrock_kb":
        return await _process_kb(doc)
    return await _process_pg(doc)


async def _process_pg(doc) -> str:
    try:
        text = extract_text(bytes(doc["content"]), doc["mime"])
    except Exception as e:
        raise PermanentError(f"could not read file: {e}")
    if len(text) < 20:
        raise PermanentError("no readable text found in file")
    embedder = get_embedder()
    chunks, chunk_tokens = await chunk_text(text, doc["chunk_strategy"], embedder)
    if not chunks:
        raise PermanentError("document produced no chunks")
    vectors: list[list[float]] = []
    tokens = chunk_tokens
    for i in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[i : i + EMBED_BATCH]
        inputs = [f"{c.heading}: {c.content}" if c.heading else c.content for c in batch]
        vecs, t = await embedder.embed(inputs, kind="document")
        vectors.extend(vecs)
        tokens += t
    tenant_id = doc["tenant_id"]
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM chunks WHERE document_id = $1", doc["id"])
            await conn.executemany(
                """
                INSERT INTO chunks (tenant_id, document_id, ord, content, context, heading, token_count, embedding)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                [(tenant_id, doc["id"], c.ord, c.content, c.context, c.heading, c.token_count, v) for c, v in zip(chunks, vectors)],
            )
            await conn.execute(
                "UPDATE documents SET status = 'ready', chunk_count = $2, error = NULL, updated_at = now() WHERE id = $1",
                doc["id"], len(chunks),
            )
            await conn.execute("UPDATE tenants SET doc_version = doc_version + 1 WHERE id = $1", tenant_id)
    await cache.bump_doc_version(str(tenant_id))
    if tokens:
        await usage.record(usage.usage_event(str(tenant_id), None, "ingest", embedder.backend, embedder.model, tokens, 0, None))
    return "ready"


def s3_keys(tenant_id: str, document_id: str, filename: str) -> tuple[str, str]:
    base = f"{get_settings().s3_prefix}/{tenant_id}/{document_id}/{filename}"
    return base, f"{base}.metadata.json"


def kb_metadata(doc) -> dict:
    return {
        "metadataAttributes": {
            "tenant_id": str(doc["tenant_id"]),
            "doc_id": str(doc["id"]),
            "title": doc["title"],
            "category": doc["category"],
        }
    }


async def _process_kb(doc) -> str:
    s = get_settings()
    key, meta_key = s3_keys(str(doc["tenant_id"]), str(doc["id"]), doc["filename"])
    content_type = {"pdf": "application/pdf", "md": "text/markdown", "txt": "text/plain"}.get(doc["mime"], "application/octet-stream")
    try:
        s3 = bedrock.client("s3")
        await asyncio.to_thread(s3.put_object, Bucket=s.s3_bucket, Key=key, Body=bytes(doc["content"]), ContentType=content_type, ServerSideEncryption="AES256")
        await asyncio.to_thread(s3.put_object, Bucket=s.s3_bucket, Key=meta_key, Body=json.dumps(kb_metadata(doc)).encode(), ContentType="application/json", ServerSideEncryption="AES256")
        await get_pool().execute("UPDATE documents SET s3_key = $2 WHERE id = $1", doc["id"], key)
        status = await sync_knowledge_base()
    except (ClientError, BotoCoreError) as e:
        raise bedrock.to_provider_error(e)
    if status != "COMPLETE":
        await spend.record_failure("bedrock")
        raise ProviderError(f"knowledge base ingestion ended with {status}", retryable=status == "TIMEOUT")
    await get_pool().execute("UPDATE documents SET status = 'ready', error = NULL, updated_at = now() WHERE id = $1", doc["id"])
    await get_pool().execute("UPDATE tenants SET doc_version = doc_version + 1 WHERE id = $1", doc["tenant_id"])
    await cache.bump_doc_version(str(doc["tenant_id"]))
    return "ready"


async def sync_knowledge_base(timeout_s: int = 900) -> str:
    s = get_settings()
    agent = bedrock.client("bedrock-agent")
    try:
        job = await asyncio.to_thread(agent.start_ingestion_job, knowledgeBaseId=s.bedrock_kb_id, dataSourceId=s.bedrock_kb_data_source_id)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConflictException":
            raise ProviderError("another ingestion job is running", retryable=True)
        raise
    job_id = job["ingestionJob"]["ingestionJobId"]
    waited = 0
    while waited < timeout_s:
        await asyncio.sleep(5)
        waited += 5
        cur = await asyncio.to_thread(agent.get_ingestion_job, knowledgeBaseId=s.bedrock_kb_id, dataSourceId=s.bedrock_kb_data_source_id, ingestionJobId=job_id)
        status = cur["ingestionJob"]["status"]
        if status in ("COMPLETE", "FAILED", "STOPPED"):
            return status
    return "TIMEOUT"


async def delete_document(tenant_id: str, document_id: str) -> bool:
    pool = get_pool()
    row = await pool.fetchrow(
        "DELETE FROM documents WHERE id = $1 AND tenant_id = $2 RETURNING s3_key",
        uuid.UUID(document_id), uuid.UUID(tenant_id),
    )
    if row is None:
        return False
    await pool.execute("UPDATE tenants SET doc_version = doc_version + 1 WHERE id = $1", uuid.UUID(tenant_id))
    await cache.bump_doc_version(tenant_id)
    if row["s3_key"]:
        s = get_settings()
        s3 = bedrock.client("s3")
        await asyncio.to_thread(
            s3.delete_objects,
            Bucket=s.s3_bucket,
            Delete={"Objects": [{"Key": row["s3_key"]}, {"Key": f"{row['s3_key']}.metadata.json"}]},
        )
        await broker.publish(INGEST, {"op": "sync", "tenant_id": tenant_id}, message_id=f"sync-{uuid.uuid4().hex}")
    return True
