from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from api.support import require_identity, resolve_image_base_url
from services.image_conversation_service import image_conversation_service


class RenameRequest(BaseModel):
    title: str


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/image-conversations")
    async def list_conversations(
        since: str = Query(default=""),
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return await run_in_threadpool(
            image_conversation_service.list_conversations,
            identity,
            since=since or None,
        )

    @router.get("/api/image-conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        result = await run_in_threadpool(
            image_conversation_service.get_conversation, identity, conversation_id
        )
        if result is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return result

    @router.post("/api/image-conversations")
    async def save_conversation(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        body = await request.json()
        try:
            return await run_in_threadpool(
                image_conversation_service.save_conversation, identity, body
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/api/image-conversations/{conversation_id}/title")
    async def rename_conversation(
        conversation_id: str,
        body: RenameRequest,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        result = await run_in_threadpool(
            image_conversation_service.rename_conversation,
            identity,
            conversation_id,
            body.title,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return result

    @router.delete("/api/image-conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        await run_in_threadpool(
            image_conversation_service.delete_conversation, identity, conversation_id
        )
        return {"ok": True}

    @router.delete("/api/image-conversations")
    async def clear_conversations(
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        count = await run_in_threadpool(
            image_conversation_service.clear_conversations, identity
        )
        return {"ok": True, "removed": count}

    @router.post("/api/image-conversations/migrate")
    async def migrate_conversations(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        body = await request.json()
        conversations = body.get("conversations", []) if isinstance(body, dict) else []
        return await run_in_threadpool(
            image_conversation_service.bulk_import, identity, conversations
        )

    @router.post("/api/image-conversations/upload-reference")
    async def upload_reference(
        request: Request,
        file: UploadFile = File(...),
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty file")
        base_url = resolve_image_base_url(request)

        def _save() -> dict[str, str]:
            from services.protocol.conversation import save_image_bytes
            url = save_image_bytes(data, base_url, prefix="input")
            return {"url": url}

        return await run_in_threadpool(_save)

    return router
