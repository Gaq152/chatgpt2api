from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from services.config import config
from services.storage.base import StorageBackend


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _sanitize_image(image: object) -> dict[str, Any] | None:
    if not isinstance(image, dict):
        return None
    sanitized: dict[str, Any] = {
        "id": _clean(image.get("id")),
    }
    if not sanitized["id"]:
        return None
    task_id = _clean(image.get("task_id") or image.get("taskId"))
    if task_id:
        sanitized["task_id"] = task_id
    status = _clean(image.get("status"))
    if status in {"loading", "success", "error"}:
        sanitized["status"] = status
    url = _clean(image.get("url"))
    if url:
        sanitized["url"] = url
    revised_prompt = _clean(image.get("revised_prompt"))
    if revised_prompt:
        sanitized["revised_prompt"] = revised_prompt
    error = _clean(image.get("error"))
    if error:
        sanitized["error"] = error
    return sanitized


def _sanitize_reference_image(ref: object) -> dict[str, Any] | None:
    if not isinstance(ref, dict):
        return None
    url = _clean(ref.get("url"))
    if not url:
        return None
    return {
        "name": _clean(ref.get("name"), "reference.png"),
        "type": _clean(ref.get("type"), "image/png"),
        "url": url,
    }


def _sanitize_turn(turn: object) -> dict[str, Any] | None:
    if not isinstance(turn, dict):
        return None
    turn_id = _clean(turn.get("id"))
    if not turn_id:
        return None
    mode = "edit" if turn.get("mode") == "edit" else "generate"
    status = _clean(turn.get("status"))
    if status not in {"queued", "generating", "success", "error"}:
        status = "success"

    images = []
    for img in (turn.get("images") or []):
        sanitized = _sanitize_image(img)
        if sanitized:
            images.append(sanitized)

    reference_images = []
    for ref in (turn.get("reference_images") or turn.get("referenceImages") or []):
        sanitized = _sanitize_reference_image(ref)
        if sanitized:
            reference_images.append(sanitized)

    result: dict[str, Any] = {
        "id": turn_id,
        "prompt": _clean(turn.get("prompt")),
        "model": _clean(turn.get("model"), "gpt-image-2"),
        "mode": mode,
        "reference_images": reference_images,
        "count": max(1, int(turn.get("count") or 1)),
        "size": _clean(turn.get("size")),
        "images": images,
        "created_at": _clean(turn.get("created_at") or turn.get("createdAt"), _now_iso()),
        "status": status,
    }
    error = _clean(turn.get("error"))
    if error:
        result["error"] = error
    if turn.get("prompt_deleted") is True or turn.get("promptDeleted") is True:
        result["prompt_deleted"] = True
    if turn.get("results_deleted") is True or turn.get("resultsDeleted") is True:
        result["results_deleted"] = True
    return result


def _sanitize_conversation(data: object, owner_id: str) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    conv_id = _clean(data.get("id"))
    if not conv_id:
        return None
    turns = []
    for turn in (data.get("turns") or []):
        sanitized = _sanitize_turn(turn)
        if sanitized:
            turns.append(sanitized)
    return {
        "id": conv_id,
        "owner_id": owner_id,
        "title": _clean(data.get("title")),
        "created_at": _clean(data.get("created_at") or data.get("createdAt"), _now_iso()),
        "updated_at": _clean(data.get("updated_at") or data.get("updatedAt"), _now_iso()),
        "turns": turns,
    }


def _public_conversation(conv: dict[str, Any]) -> dict[str, Any]:
    result = dict(conv)
    result.pop("owner_id", None)
    return result


class ImageConversationService:
    def __init__(self, storage: StorageBackend):
        self.storage = storage
        self._lock = threading.RLock()
        self._items: list[dict[str, Any]] = self._load()
        self._flush_at: float = 0.0

    def _load(self) -> list[dict[str, Any]]:
        try:
            items = self.storage.load_image_conversations()
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict) and _clean(item.get("id"))]

    def _save(self) -> None:
        self.storage.save_image_conversations(self._items)

    def _save_throttled(self, force: bool = False) -> None:
        now = time.time()
        if force or (now - self._flush_at) >= 5:
            try:
                self._save()
                self._flush_at = now
            except Exception:
                pass

    def _cleanup_locked(self) -> bool:
        try:
            retention_days = max(1, config.image_retention_days)
        except Exception:
            retention_days = 30
        cutoff = time.time() - retention_days * 86400
        before = len(self._items)
        self._items = [
            item for item in self._items
            if _timestamp(item.get("updated_at")) >= cutoff
        ]
        return len(self._items) < before

    def list_conversations(self, identity: dict[str, object], *, since: str | None = None) -> dict[str, Any]:
        owner = _owner_id(identity)
        since_ts = _timestamp(since) if since else 0.0
        with self._lock:
            if self._cleanup_locked():
                self._save_throttled(force=True)
            items = [
                _public_conversation(item)
                for item in self._items
                if item.get("owner_id") == owner
                and (since_ts == 0.0 or _timestamp(item.get("updated_at")) > since_ts)
            ]
        items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
        return {"items": items}

    def get_conversation(self, identity: dict[str, object], conversation_id: str) -> dict[str, Any] | None:
        owner = _owner_id(identity)
        normalized_id = _clean(conversation_id)
        if not normalized_id:
            return None
        with self._lock:
            for item in self._items:
                if item.get("id") == normalized_id and item.get("owner_id") == owner:
                    return _public_conversation(item)
        return None

    def save_conversation(self, identity: dict[str, object], data: dict[str, Any]) -> dict[str, Any]:
        owner = _owner_id(identity)
        sanitized = _sanitize_conversation(data, owner)
        if sanitized is None:
            raise ValueError("无效的会话数据")
        with self._lock:
            for index, item in enumerate(self._items):
                if item.get("id") == sanitized["id"] and item.get("owner_id") == owner:
                    self._items[index] = sanitized
                    self._save_throttled(force=True)
                    return _public_conversation(sanitized)
            self._items.append(sanitized)
            self._save_throttled(force=True)
            return _public_conversation(sanitized)

    def rename_conversation(
        self, identity: dict[str, object], conversation_id: str, title: str
    ) -> dict[str, Any] | None:
        owner = _owner_id(identity)
        normalized_id = _clean(conversation_id)
        if not normalized_id:
            return None
        with self._lock:
            for index, item in enumerate(self._items):
                if item.get("id") == normalized_id and item.get("owner_id") == owner:
                    updated = dict(item)
                    updated["title"] = _clean(title)
                    updated["updated_at"] = _now_iso()
                    self._items[index] = updated
                    self._save_throttled(force=True)
                    return _public_conversation(updated)
        return None

    def delete_conversation(self, identity: dict[str, object], conversation_id: str) -> bool:
        owner = _owner_id(identity)
        normalized_id = _clean(conversation_id)
        if not normalized_id:
            return False
        with self._lock:
            before = len(self._items)
            self._items = [
                item for item in self._items
                if not (item.get("id") == normalized_id and item.get("owner_id") == owner)
            ]
            if len(self._items) == before:
                return False
            self._save_throttled(force=True)
            return True

    def clear_conversations(self, identity: dict[str, object]) -> int:
        owner = _owner_id(identity)
        with self._lock:
            before = len(self._items)
            self._items = [
                item for item in self._items
                if item.get("owner_id") != owner
            ]
            removed = before - len(self._items)
            if removed > 0:
                self._save_throttled(force=True)
            return removed

    def clear_conversations_by_owner(self, owner_id: str) -> int:
        normalized = _clean(owner_id)
        if not normalized:
            return 0
        with self._lock:
            before = len(self._items)
            self._items = [
                item for item in self._items
                if item.get("owner_id") != normalized
            ]
            removed = before - len(self._items)
            if removed > 0:
                self._save_throttled(force=True)
            return removed

    def cleanup_orphaned_owners(self, valid_owner_ids: set[str]) -> int:
        if not valid_owner_ids:
            return 0
        with self._lock:
            before = len(self._items)
            self._items = [
                item for item in self._items
                if item.get("owner_id") in valid_owner_ids
            ]
            removed = before - len(self._items)
            if removed > 0:
                self._save_throttled(force=True)
            return removed

    def bulk_import(
        self, identity: dict[str, object], conversations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        owner = _owner_id(identity)
        if not isinstance(conversations, list):
            return {"imported": 0, "skipped": 0}
        imported = 0
        skipped = 0
        with self._lock:
            existing_ids = {
                item.get("id")
                for item in self._items
                if item.get("owner_id") == owner
            }
            for raw in conversations:
                sanitized = _sanitize_conversation(raw, owner)
                if sanitized is None:
                    skipped += 1
                    continue
                if sanitized["id"] in existing_ids:
                    skipped += 1
                    continue
                self._items.append(sanitized)
                existing_ids.add(sanitized["id"])
                imported += 1
            if imported > 0:
                self._save_throttled(force=True)
        return {"imported": imported, "skipped": skipped}


image_conversation_service = ImageConversationService(config.get_storage_backend())
