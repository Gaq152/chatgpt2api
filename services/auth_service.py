from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Literal

from services.config import config
from services.storage.base import StorageBackend

AuthRole = Literal["admin", "user"]

QUOTA_WINDOW = timedelta(hours=24)


class QuotaExceededError(Exception):
    """普通用户在 24h 滚动窗口内已用完配额。reset_at 为 ISO 字符串。"""

    def __init__(self, reset_at: str) -> None:
        super().__init__(f"quota exceeded, reset at {reset_at}")
        self.reset_at = reset_at


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(self, storage: StorageBackend):
        self.storage = storage
        self._lock = Lock()
        self._items = self._load()
        self._last_used_flush_at: dict[str, datetime] = {}
        self._quota_flush_at: dict[str, datetime] = {}

    @staticmethod
    def _clean(value: object) -> str:
        return str(value or "").strip()

    @staticmethod
    def _default_name(role: object) -> str:
        return "管理员密钥" if str(role or "").strip().lower() == "admin" else "普通用户"

    @staticmethod
    def _normalize_quota(value: object) -> int | None:
        if value is None:
            return None
        try:
            quota = int(value)
        except (TypeError, ValueError):
            return None
        return quota if quota > 0 else None

    def _normalize_item(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        role = self._clean(raw.get("role")).lower()
        if role not in {"admin", "user"}:
            return None
        key_hash = self._clean(raw.get("key_hash"))
        if not key_hash:
            return None
        item_id = self._clean(raw.get("id")) or uuid.uuid4().hex[:12]
        name = self._clean(raw.get("name")) or self._default_name(role)
        created_at = self._clean(raw.get("created_at")) or _now_iso()
        last_used_at = self._clean(raw.get("last_used_at")) or None
        item: dict[str, object] = {
            "id": item_id,
            "name": name,
            "role": role,
            "key_hash": key_hash,
            "enabled": bool(raw.get("enabled", True)),
            "created_at": created_at,
            "last_used_at": last_used_at,
        }
        if role == "user":
            item["quota_24h"] = self._normalize_quota(raw.get("quota_24h"))
            item["quota_used"] = max(0, int(raw.get("quota_used") or 0))
            item["quota_window_start"] = self._clean(raw.get("quota_window_start")) or None
        return item

    def _load(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_auth_keys()
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [normalized for item in items if (normalized := self._normalize_item(item)) is not None]

    def _save(self) -> None:
        self.storage.save_auth_keys(self._items)

    def _reload_locked(self) -> None:
        self._items = self._load()

    @staticmethod
    def _public_item(item: dict[str, object]) -> dict[str, object]:
        public: dict[str, object] = {
            "id": item.get("id"),
            "name": item.get("name"),
            "role": item.get("role"),
            "enabled": bool(item.get("enabled", True)),
            "created_at": item.get("created_at"),
            "last_used_at": item.get("last_used_at"),
        }
        if str(item.get("role") or "").strip().lower() == "user":
            quota = item.get("quota_24h")
            window_start = item.get("quota_window_start")
            used = int(item.get("quota_used") or 0)
            public["quota_24h"] = quota
            public["quota_used"] = used
            public["quota_window_start"] = window_start
            reset_at = None
            parsed = _parse_iso(window_start)
            if parsed is not None:
                reset_at = (parsed + QUOTA_WINDOW).isoformat()
            public["quota_reset_at"] = reset_at
        return public

    def list_keys(self, role: AuthRole | None = None) -> list[dict[str, object]]:
        with self._lock:
            self._reload_locked()
            items = [item for item in self._items if role is None or item.get("role") == role]
            return [self._public_item(item) for item in items]

    def _has_key_hash_locked(self, key_hash: str, *, exclude_id: str = "") -> bool:
        for item in self._items:
            item_id = self._clean(item.get("id"))
            if exclude_id and item_id == exclude_id:
                continue
            stored_hash = self._clean(item.get("key_hash"))
            if stored_hash and hmac.compare_digest(stored_hash, key_hash):
                return True
        return False

    def _build_key_hash_locked(self, raw_key: str, *, exclude_id: str = "") -> str:
        candidate = self._clean(raw_key)
        if not candidate:
            raise ValueError("请输入新的专用密钥")
        admin_key = self._clean(config.auth_key)
        if admin_key and hmac.compare_digest(candidate, admin_key):
            raise ValueError("这个密钥和管理员密钥冲突了，请换一个新的密钥")
        key_hash = _hash_key(candidate)
        if self._has_key_hash_locked(key_hash, exclude_id=exclude_id):
            raise ValueError("这个专用密钥已经存在，请换一个新的密钥")
        return key_hash

    def _has_name_locked(self, name: str, *, role: AuthRole | None = None, exclude_id: str = "") -> bool:
        candidate = self._clean(name)
        if not candidate:
            return False
        for item in self._items:
            item_id = self._clean(item.get("id"))
            if exclude_id and item_id == exclude_id:
                continue
            if role is not None and item.get("role") != role:
                continue
            if self._clean(item.get("name")) == candidate:
                return True
        return False

    def _build_default_name_locked(self, role: AuthRole, *, exclude_id: str = "") -> str:
        base_name = self._default_name(role)
        if not self._has_name_locked(base_name, role=role, exclude_id=exclude_id):
            return base_name
        suffix = 2
        while True:
            candidate = f"{base_name} {suffix}"
            if not self._has_name_locked(candidate, role=role, exclude_id=exclude_id):
                return candidate
            suffix += 1

    def _build_name_locked(self, name: str, *, role: AuthRole, exclude_id: str = "") -> str:
        candidate = self._clean(name)
        if not candidate:
            return self._build_default_name_locked(role, exclude_id=exclude_id)
        if self._has_name_locked(candidate, role=role, exclude_id=exclude_id):
            raise ValueError("这个名称已经在使用中了，换一个更容易区分的名称吧")
        return candidate

    def create_key(self, *, role: AuthRole, name: str = "", quota_24h: object = None) -> tuple[dict[str, object], str]:
        with self._lock:
            self._reload_locked()
            normalized_name = self._build_name_locked(name, role=role)
            quota_value: int | None = None
            if role == "user":
                quota_value = self._normalize_quota(quota_24h)
                if quota_value is None:
                    raise ValueError("普通用户密钥必须配置 24 小时调用上限（quota_24h）")
            while True:
                raw_key = f"sk-{secrets.token_urlsafe(24)}"
                try:
                    key_hash = self._build_key_hash_locked(raw_key)
                    break
                except ValueError:
                    continue
            item: dict[str, object] = {
                "id": uuid.uuid4().hex[:12],
                "name": normalized_name,
                "role": role,
                "key_hash": key_hash,
                "enabled": True,
                "created_at": _now_iso(),
                "last_used_at": None,
            }
            if role == "user":
                item["quota_24h"] = quota_value
                item["quota_used"] = 0
                item["quota_window_start"] = None
            self._items.append(item)
            self._save()
            return self._public_item(item), raw_key

    def update_key(
        self,
        key_id: str,
        updates: dict[str, object],
        *,
        role: AuthRole | None = None,
    ) -> dict[str, object] | None:
        normalized_id = self._clean(key_id)
        if not normalized_id:
            return None
        with self._lock:
            self._reload_locked()
            for index, item in enumerate(self._items):
                if item.get("id") != normalized_id:
                    continue
                if role is not None and item.get("role") != role:
                    return None
                next_item = dict(item)
                next_role = "admin" if str(next_item.get("role") or "").strip().lower() == "admin" else "user"
                if "name" in updates and updates.get("name") is not None:
                    next_item["name"] = self._build_name_locked(
                        str(updates.get("name") or ""),
                        role=next_role,
                        exclude_id=normalized_id,
                    )
                if "enabled" in updates and updates.get("enabled") is not None:
                    next_item["enabled"] = bool(updates.get("enabled"))
                if "key" in updates and updates.get("key") is not None:
                    next_item["key_hash"] = self._build_key_hash_locked(str(updates.get("key") or ""), exclude_id=normalized_id)
                if next_role == "user" and "quota_24h" in updates and updates.get("quota_24h") is not None:
                    quota_value = self._normalize_quota(updates.get("quota_24h"))
                    if quota_value is None:
                        raise ValueError("quota_24h 必须为正整数")
                    next_item["quota_24h"] = quota_value
                self._items[index] = next_item
                self._save()
                return self._public_item(next_item)
        return None

    def delete_key(self, key_id: str, *, role: AuthRole | None = None) -> bool:
        normalized_id = self._clean(key_id)
        if not normalized_id:
            return False
        with self._lock:
            self._reload_locked()
            before = len(self._items)
            self._items = [
                item
                for item in self._items
                if not (item.get("id") == normalized_id and (role is None or item.get("role") == role))
            ]
            if len(self._items) == before:
                return False
            self._save()
            return True

    def authenticate(self, raw_key: str) -> dict[str, object] | None:
        candidate = self._clean(raw_key)
        if not candidate:
            return None
        candidate_hash = _hash_key(candidate)
        with self._lock:
            for index, item in enumerate(self._items):
                if not bool(item.get("enabled", True)):
                    continue
                stored_hash = self._clean(item.get("key_hash"))
                if not stored_hash or not hmac.compare_digest(stored_hash, candidate_hash):
                    continue
                next_item = dict(item)
                now = datetime.now(timezone.utc)
                next_item["last_used_at"] = now.isoformat()
                self._items[index] = next_item
                item_id = self._clean(next_item.get("id"))
                last_flush_at = self._last_used_flush_at.get(item_id)
                if last_flush_at is None or (now - last_flush_at).total_seconds() >= 60:
                    try:
                        self._save()
                        self._last_used_flush_at[item_id] = now
                    except Exception:
                        pass
                return self._public_item(next_item)
        return None

    def peek_quota(self, key_id: str) -> dict[str, object] | None:
        """检查 key 在当前 24h 滚动窗口内是否还有额度。

        admin 角色返回 None（视为无限制）；user 角色未配 quota_24h 也返回 None（兼容旧数据）。
        返回 dict 含 remaining/reset_at 信息，调用方据此判断是否抛 QuotaExceededError。
        """
        normalized_id = self._clean(key_id)
        if not normalized_id:
            return None
        with self._lock:
            for item in self._items:
                if self._clean(item.get("id")) != normalized_id:
                    continue
                if str(item.get("role") or "").strip().lower() != "user":
                    return None
                quota = item.get("quota_24h")
                if not isinstance(quota, int) or quota <= 0:
                    return None
                window_start = _parse_iso(item.get("quota_window_start"))
                used = int(item.get("quota_used") or 0)
                now = datetime.now(timezone.utc)
                if window_start is None or (now - window_start) >= QUOTA_WINDOW:
                    return {"remaining": quota, "reset_at": None, "fresh": True, "quota_24h": quota}
                reset_at = (window_start + QUOTA_WINDOW).isoformat()
                return {
                    "remaining": max(0, quota - used),
                    "reset_at": reset_at,
                    "fresh": False,
                    "quota_24h": quota,
                }
        return None

    def consume_quota(self, key_id: str, count: int = 1) -> None:
        """成功 / 因用户自身原因失败时调用一次扣减。

        admin 或未配额的 user 直接 no-op；超额时抛 QuotaExceededError。
        24h 后未使用 → 本次重新起算窗口（不顺延）。
        计数走 60s 批量刷盘以减少写盘次数。
        """
        normalized_id = self._clean(key_id)
        if not normalized_id or count <= 0:
            return
        with self._lock:
            for index, item in enumerate(self._items):
                if self._clean(item.get("id")) != normalized_id:
                    continue
                if str(item.get("role") or "").strip().lower() != "user":
                    return
                quota = item.get("quota_24h")
                if not isinstance(quota, int) or quota <= 0:
                    return
                next_item = dict(item)
                window_start = _parse_iso(next_item.get("quota_window_start"))
                used = int(next_item.get("quota_used") or 0)
                now = datetime.now(timezone.utc)
                if window_start is None or (now - window_start) >= QUOTA_WINDOW:
                    window_start = now
                    used = 0
                if used + count > quota:
                    reset_at = (window_start + QUOTA_WINDOW).isoformat()
                    raise QuotaExceededError(reset_at)
                used += count
                next_item["quota_window_start"] = window_start.isoformat()
                next_item["quota_used"] = used
                self._items[index] = next_item
                last_flush = self._quota_flush_at.get(normalized_id)
                if last_flush is None or (now - last_flush).total_seconds() >= 60:
                    try:
                        self._save()
                        self._quota_flush_at[normalized_id] = now
                    except Exception:
                        pass
                return


auth_service = AuthService(config.get_storage_backend())
