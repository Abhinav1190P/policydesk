import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.config import get_settings
from app.db.pool import get_pool
from app.infra.broker import INGEST, broker
from app.security import CurrentUser, admin_user, current_user
from app.services import ingest

router = APIRouter(prefix="/api/documents", tags=["documents"])

COLUMNS = "id, title, filename, mime, category, status, chunk_strategy, chunk_count, attempts, error, size_bytes, created_at, updated_at"


def doc_out(r) -> dict:
    d = dict(r)
    d["id"] = str(d["id"])
    return d


@router.post("", status_code=202)
async def upload(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    category: str = Form(default="general"),
    user: CurrentUser = Depends(admin_user),
):
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    try:
        return await ingest.create_document(user.tenant_id, user.id, file.filename or "upload.txt", file.content_type, data, title, category)
    except ingest.UploadError as e:
        raise HTTPException(e.status, str(e))


@router.get("")
async def list_documents(user: CurrentUser = Depends(current_user)):
    rows = await get_pool().fetch(
        f"SELECT {COLUMNS} FROM documents WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT 200",
        uuid.UUID(user.tenant_id),
    )
    return [doc_out(r) for r in rows]


@router.get("/{document_id}")
async def get_document(document_id: uuid.UUID, user: CurrentUser = Depends(current_user)):
    row = await get_pool().fetchrow(f"SELECT {COLUMNS} FROM documents WHERE id = $1 AND tenant_id = $2", document_id, uuid.UUID(user.tenant_id))
    if row is None:
        raise HTTPException(404, "Document not found")
    return doc_out(row)


@router.post("/{document_id}/retry", status_code=202)
async def retry(document_id: uuid.UUID, user: CurrentUser = Depends(admin_user)):
    row = await get_pool().fetchrow(
        "UPDATE documents SET status = 'queued', error = NULL, updated_at = now() WHERE id = $1 AND tenant_id = $2 AND status = 'failed' RETURNING id",
        document_id, uuid.UUID(user.tenant_id),
    )
    if row is None:
        raise HTTPException(409, "Only failed documents can be retried")
    await broker.publish(INGEST, {"op": "ingest", "document_id": str(document_id), "tenant_id": user.tenant_id})
    return {"id": str(document_id), "status": "queued"}


@router.delete("/{document_id}", status_code=204)
async def delete(document_id: uuid.UUID, user: CurrentUser = Depends(admin_user)):
    if not await ingest.delete_document(user.tenant_id, str(document_id)):
        raise HTTPException(404, "Document not found")
