import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db.pool import close_pool, create_pool, get_pool
from app.infra import cache
from app.infra.redis_client import close_redis, create_redis
from app.providers import bedrock
from app.services.ingest import kb_metadata, s3_keys, sync_knowledge_base

CONTENT_TYPES = {"pdf": "application/pdf", "md": "text/markdown", "txt": "text/plain"}


async def main() -> None:
    s = get_settings()
    if not (s.s3_bucket and s.bedrock_kb_id and s.bedrock_kb_data_source_id):
        sys.exit("set S3_BUCKET, BEDROCK_KB_ID and BEDROCK_KB_DATA_SOURCE_ID first")
    await create_pool()
    await create_redis()
    s3 = bedrock.client("s3")
    rows = await get_pool().fetch("SELECT id, tenant_id, title, filename, mime, category, content FROM documents WHERE status = 'ready'")
    for doc in rows:
        key, meta_key = s3_keys(str(doc["tenant_id"]), str(doc["id"]), doc["filename"])
        await asyncio.to_thread(s3.put_object, Bucket=s.s3_bucket, Key=key, Body=bytes(doc["content"]), ContentType=CONTENT_TYPES.get(doc["mime"], "application/octet-stream"), ServerSideEncryption="AES256")
        await asyncio.to_thread(s3.put_object, Bucket=s.s3_bucket, Key=meta_key, Body=json.dumps(kb_metadata(doc)).encode(), ContentType="application/json", ServerSideEncryption="AES256")
        await get_pool().execute("UPDATE documents SET s3_key = $2 WHERE id = $1", doc["id"], key)
        print(f"uploaded {doc['title']}")
    print("syncing knowledge base (takes a minute or two)...")
    status = await sync_knowledge_base()
    print(f"ingestion job: {status}")
    for t in {str(r["tenant_id"]) for r in rows}:
        await cache.bump_doc_version(t)
    await close_redis()
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
