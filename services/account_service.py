from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Condition, Lock
from typing import Any
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.config import config
from services.log_service import (
    LOG_TYPE_ACCOUNT,
    log_service,
)
from services.storage.base import StorageBackend
from utils.helper import anonymize_token


class AccountService:
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    def __init__(self, storage_backend: StorageBackend):
        self.storage = storage_backend
        self._lock = Lock()
        self._image_slot_condition = Condition(self._lock)
        self._index = 0
        self._accounts = self._load_accounts()
        self._image_inflight: dict[str, int] = {}
        self._cumulative_total = self._load_cumulative_total()

    def _get_cumulative_file(self) -> Path:
        from services.config import DATA_DIR
        return DATA_DIR / ".cumulative_total"

    def _load_cumulative_total(self) -> int:
        try:
            f = self._get_cumulative_file()
            if f.exists():
                return int(f.read_text().strip())
        except Exception:
            pass
        return len(self._accounts)

    def _save_cumulative_total(self) -> None:
        try:
            self._get_cumulative_file().write_text(str(self._cumulative_total))
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _load_accounts(self) -> dict[str, dict]:
        accounts = self.storage.load_accounts()
        return {
            normalized["access_token"]: normalized
            for item in accounts
            if (normalized := self._normalize_account(item)) is not None
        }

    def _save_accounts(self) -> None:
        self.storage.save_accounts(list(self._accounts.values()))

    @staticmethod
    def _is_image_account_available(account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {"禁用", "限流", "异常"}:
            return False
        if bool(account.get("image_quota_unknown")):
            return True
        return int(account.get("quota") or 0) > 0

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized["access_token"] = access_token
        normalized["type"] = normalized.get("type") or "free"
        normalized["status"] = normalized.get("status") or "正常"
        normalized["quota"] = max(0, int(normalized.get("quota") if normalized.get("quota") is not None else 0))
        normalized["image_quota_unknown"] = bool(normalized.get("image_quota_unknown"))
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        limits_progress = normalized.get("limits_progress")
        normalized["limits_progress"] = limits_progress if isinstance(limits_progress, list) else []
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        normalized["created_at"] = normalized.get("created_at") or AccountService._now()
        normalized["refresh_token"] = (normalized.get("refresh_token") or "") or None
        normalized["id_token"] = (normalized.get("id_token") or "") or None
        normalized["password"] = (normalized.get("password") or "") or None
        normalized["account_id"] = (normalized.get("account_id") or "") or None
        normalized["export_type"] = (normalized.get("export_type") or "") or None
        source = str(normalized.get("source") or "manual").strip().lower()
        if source not in {"manual", "cpa", "sub2api", "register"}:
            source = "manual"
        normalized["source"] = source
        normalized["source_account_id"] = (normalized.get("source_account_id") or "") or None
        normalized["source_pool_id"] = (normalized.get("source_pool_id") or "") or None
        normalized["source_pool_file"] = (normalized.get("source_pool_file") or "") or None
        normalized["source_server_id"] = (normalized.get("source_server_id") or "") or None
        return normalized

    def list_tokens(self) -> list[str]:
        with self._lock:
            return list(self._accounts)

    def _list_ready_candidate_tokens(self, excluded_tokens: set[str] | None = None) -> list[str]:
        excluded = set(excluded_tokens or set())
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
               and (token := item.get("access_token") or "")
               and token not in excluded
        ]

    def _list_available_candidate_tokens(self, excluded_tokens: set[str] | None = None) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(excluded_tokens)
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    def _acquire_next_candidate_token(self, excluded_tokens: set[str] | None = None) -> str:
        with self._image_slot_condition:
            while True:
                if not self._list_ready_candidate_tokens(excluded_tokens):
                    raise RuntimeError("no available image quota")
                tokens = self._list_available_candidate_tokens(excluded_tokens)
                if tokens:
                    access_token = tokens[self._index % len(tokens)]
                    self._index += 1
                    self._image_inflight[access_token] = int(self._image_inflight.get(access_token, 0)) + 1
                    return access_token
                self._image_slot_condition.wait(timeout=1.0)

    def release_image_slot(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            current_inflight = int(self._image_inflight.get(access_token, 0))
            if current_inflight <= 1:
                self._image_inflight.pop(access_token, None)
            else:
                self._image_inflight[access_token] = current_inflight - 1
            self._image_slot_condition.notify_all()

    def get_available_access_token(self) -> str:
        attempted_tokens: set[str] = set()
        while True:
            access_token = self._acquire_next_candidate_token(excluded_tokens=attempted_tokens)
            attempted_tokens.add(access_token)
            try:
                account = self.fetch_remote_info(access_token, "get_available_access_token")
            except Exception:
                self.release_image_slot(access_token)
                continue
            if self._is_image_account_available(account or {}):
                return access_token
            self.release_image_slot(access_token)

    def get_text_access_token(self, excluded_tokens: set[str] | None = None) -> str:
        excluded = set(excluded_tokens or set())
        with self._lock:
            candidates = [
                token
                for account in self._accounts.values()
                if account.get("status") not in {"禁用", "异常"}
                   and (token := account.get("access_token") or "")
                   and token not in excluded
            ]
            if not candidates:
                return ""
            access_token = candidates[self._index % len(candidates)]
            self._index += 1
            return access_token

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._save_accounts()

    def remove_invalid_token(self, access_token: str, event: str) -> bool:
        if not config.auto_remove_invalid_accounts:
            self.update_account(access_token, {"status": "异常", "quota": 0})
            return False
        removed = bool(self.delete_accounts([access_token])["removed"])
        if removed:
            log_service.add(LOG_TYPE_ACCOUNT, "自动移除异常账号",
                            {"source": event, "token": anonymize_token(access_token)})
        elif access_token:
            self.update_account(access_token, {"status": "异常", "quota": 0})
        return removed

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def list_accounts(self) -> list[dict]:
        """对外列表，剥掉 password 等只持久化、不该回显前端的敏感字段。"""
        with self._lock:
            return [
                {key: value for key, value in item.items() if key != "password"}
                for item in self._accounts.values()
            ]

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        try:
            parts = str(token or "").split(".")
            if len(parts) < 2:
                return {}
            payload = parts[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += "=" * padding
            return json.loads(base64.urlsafe_b64decode(payload))
        except Exception:
            return {}

    @staticmethod
    def _format_iso_cn(timestamp: object) -> str:
        try:
            seconds = int(timestamp)
        except (TypeError, ValueError):
            return ""
        try:
            return datetime.fromtimestamp(seconds, tz=timezone(timedelta(hours=8))).isoformat()
        except (OSError, OverflowError, ValueError):
            return ""

    def build_export_items(self, access_tokens: list[str] | None = None) -> list[dict]:
        """codex 风格导出。仅返回三件套齐全的账号；password 若已存就一并带上。"""
        with self._lock:
            if access_tokens:
                requested = {token for token in access_tokens if token}
                source_items = [self._accounts[token] for token in requested if token in self._accounts]
            else:
                source_items = list(self._accounts.values())

        items: list[dict] = []
        for account in source_items:
            access_token = str(account.get("access_token") or "").strip()
            refresh_token = str(account.get("refresh_token") or "").strip()
            id_token = str(account.get("id_token") or "").strip()
            if not access_token or not refresh_token or not id_token:
                continue

            access_payload = self._decode_jwt_payload(access_token)
            id_payload = self._decode_jwt_payload(id_token)

            profile = access_payload.get("https://api.openai.com/profile") if isinstance(access_payload.get("https://api.openai.com/profile"), dict) else {}
            auth_claim = access_payload.get("https://api.openai.com/auth") if isinstance(access_payload.get("https://api.openai.com/auth"), dict) else {}

            email = (
                str(profile.get("email") or "").strip()
                or str(id_payload.get("email") or "").strip()
                or str(account.get("email") or "").strip()
            )
            account_id = str(auth_claim.get("chatgpt_account_id") or account.get("account_id") or "").strip()
            expired = self._format_iso_cn(access_payload.get("exp"))
            last_refresh = self._format_iso_cn(access_payload.get("iat"))

            item: dict[str, object] = {
                "type": "codex",
                "email": email,
                "expired": expired,
                "account_id": account_id,
                "access_token": access_token,
                "last_refresh": last_refresh,
                "id_token": id_token,
                "refresh_token": refresh_token,
            }
            password = str(account.get("password") or "").strip()
            if password:
                item["password"] = password
            items.append(item)
        return items

    def list_limited_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "限流"
                   and (token := item.get("access_token") or "")
            ]

    _ACCOUNT_PRESERVE_FIELDS = (
        "access_token",
        "refresh_token",
        "id_token",
        "password",
        "email",
        "type",
        "export_type",
        "account_id",
        "source",
        "source_account_id",
        "source_pool_id",
        "source_pool_file",
        "source_server_id",
    )

    def add_account_items(self, items: list[dict]) -> dict:
        """保留每条账号的完整字段写入号池。仅 access_token 字符串可走 add_accounts。"""
        cleaned: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            access_token = str(item.get("access_token") or "").strip()
            if not access_token:
                continue
            entry = {
                key: item.get(key)
                for key in self._ACCOUNT_PRESERVE_FIELDS
                if item.get(key) is not None
            }
            entry["access_token"] = access_token
            # 导入文件用 type=codex 标识来源属于 codex 导出文件，但号池里的 type
            # 字段表示订阅类型（free/plus/...），所以这里把它改名到 export_type 避免覆盖。
            if str(entry.get("type") or "").strip().lower() == "codex":
                entry["export_type"] = "codex"
                entry.pop("type", None)
            cleaned.append(entry)
        if not cleaned:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}

        with self._lock:
            added = 0
            skipped = 0
            seen: set[str] = set()
            for incoming in cleaned:
                access_token = incoming["access_token"]
                if access_token in seen:
                    continue
                seen.add(access_token)
                current = self._accounts.get(access_token)
                if current is None:
                    added += 1
                    self._cumulative_total += 1
                    self._save_cumulative_total()
                    base = {"created_at": self._now()}
                else:
                    skipped += 1
                    base = dict(current)
                merged = {**base, **incoming}
                account = self._normalize_account(merged)
                if account is not None:
                    self._accounts[access_token] = account
            self._save_accounts()
            items_out = [dict(item) for item in self._accounts.values()]
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped})
        return {"added": added, "skipped": skipped, "items": items_out}

    def add_accounts(self, tokens: list[str]) -> dict:
        tokens = list(dict.fromkeys(token for token in tokens if token))
        if not tokens:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}

        with self._lock:
            added = 0
            skipped = 0
            for access_token in tokens:
                current = self._accounts.get(access_token)
                if current is None:
                    added += 1
                    self._cumulative_total += 1
                    self._save_cumulative_total()
                    current = {"created_at": self._now()}
                else:
                    skipped += 1
                account = self._normalize_account(
                    {
                        **current,
                        "access_token": access_token,
                        "type": str(current.get("type") or "free"),
                    }
                )
                if account is not None:
                    self._accounts[access_token] = account
            self._save_accounts()
            items = [dict(item) for item in self._accounts.values()]
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped})
        return {"added": added, "skipped": skipped, "items": items}

    def delete_accounts(self, tokens: list[str]) -> dict:
        target_set = set(token for token in tokens if token)
        if not target_set:
            return {"removed": 0, "items": self.list_accounts()}
        with self._lock:
            removed = sum(self._accounts.pop(token, None) is not None for token in target_set)
            for token in target_set:
                self._image_inflight.pop(token, None)
            if removed:
                if self._accounts:
                    self._index %= len(self._accounts)
                else:
                    self._index = 0
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, f"删除 {removed} 个账号", {"removed": removed})
            items = [dict(item) for item in self._accounts.values()]
        return {"removed": removed, "items": items}

    def update_account(self, access_token: str, updates: dict) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return None
            account = self._normalize_account({**current, **updates, "access_token": access_token})
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                            {"token": anonymize_token(access_token), "status": account.get("status")})
            return dict(account)
        return None

    def mark_image_result(self, access_token: str, success: bool) -> dict | None:
        if not access_token:
            return None
        self.release_image_slot(access_token)
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            image_quota_unknown = bool(next_item.get("image_quota_unknown"))
            if success:
                next_item["success"] = int(next_item.get("success") or 0) + 1
                if not image_quota_unknown:
                    next_item["quota"] = max(0, int(next_item.get("quota") or 0) - 1)
                if not image_quota_unknown and next_item["quota"] == 0:
                    next_item["status"] = "限流"
                    next_item["restore_at"] = next_item.get("restore_at") or None
                elif next_item.get("status") == "限流":
                    next_item["status"] = "正常"
            else:
                next_item["fail"] = int(next_item.get("fail") or 0) + 1
            account = self._normalize_account(next_item)
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            return dict(account)
        return None

    def fetch_remote_info(self, access_token: str, event: str = "fetch_remote_info") -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        try:
            from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI
            result = OpenAIBackendAPI(access_token).get_user_info()
        except InvalidAccessTokenError:
            self.remove_invalid_token(access_token, event)
            raise
        return self.update_account(access_token, result)

    def _swap_access_token(self, old_token: str, updates: dict) -> str:
        """把账号在内存 dict 中的 key 从 old_token 换成 updates['access_token']。

        token 三件套刷新时 access_token 通常会变；refresh_token 也会被服务端轮换。
        返回新 token；若 access_token 没变则原样返回。
        """
        new_token = str(updates.get("access_token") or "").strip()
        if not new_token:
            return old_token
        with self._lock:
            current = self._accounts.get(old_token)
            if current is None:
                return old_token
            merged = {**current, **updates, "access_token": new_token}
            account = self._normalize_account(merged)
            if account is None:
                return old_token
            if new_token != old_token:
                self._accounts.pop(old_token, None)
                inflight = self._image_inflight.pop(old_token, None)
                if inflight:
                    self._image_inflight[new_token] = inflight
            self._accounts[new_token] = account
            self._save_accounts()
            return new_token

    def _refresh_one_by_source(self, access_token: str) -> str:
        """按 source 走差异化刷新，返回当前生效的 access_token（可能被替换）。

        任何刷新策略失败都会回退到 fetch_remote_info（仅刷额度），保证流程不中断。
        """
        with self._lock:
            account = self._accounts.get(access_token)
            snapshot = dict(account) if account else None
        if snapshot is None:
            return access_token

        source = str(snapshot.get("source") or "manual")
        try:
            if source in {"manual", "register"}:
                refresh_token = str(snapshot.get("refresh_token") or "").strip()
                if refresh_token:
                    from services.register.openai_register import refresh_platform_tokens
                    new_tokens = refresh_platform_tokens(refresh_token)
                    if new_tokens and new_tokens.get("access_token"):
                        updates: dict[str, Any] = {"access_token": new_tokens["access_token"]}
                        if new_tokens.get("refresh_token"):
                            updates["refresh_token"] = new_tokens["refresh_token"]
                        if new_tokens.get("id_token"):
                            updates["id_token"] = new_tokens["id_token"]
                        access_token = self._swap_access_token(access_token, updates)
            elif source == "sub2api":
                server_id = str(snapshot.get("source_server_id") or "").strip()
                account_id = str(snapshot.get("source_account_id") or "").strip()
                if server_id and account_id:
                    from services.sub2api_service import _fetch_access_token_for_account, sub2api_config
                    server = sub2api_config.get_server(server_id)
                    if server:
                        new_token, meta = _fetch_access_token_for_account(server, account_id)
                        if new_token:
                            updates = {"access_token": new_token}
                            if meta.get("refresh_token"):
                                updates["refresh_token"] = meta["refresh_token"]
                            if meta.get("id_token"):
                                updates["id_token"] = meta["id_token"]
                            if meta.get("email"):
                                updates["email"] = meta["email"]
                            access_token = self._swap_access_token(access_token, updates)
            elif source == "cpa":
                pool_id = str(snapshot.get("source_pool_id") or "").strip()
                file_name = (
                    str(snapshot.get("source_pool_file") or "").strip()
                    or str(snapshot.get("email") or "").strip()  # 兼容老数据：早期没存 source_pool_file
                )
                if pool_id and file_name:
                    from services.cpa_service import cpa_config, fetch_remote_account
                    pool = cpa_config.get_pool(pool_id)
                    if pool:
                        new_token, meta, _err = fetch_remote_account(pool, file_name)
                        if new_token:
                            updates = {"access_token": new_token}
                            for key in ("refresh_token", "id_token", "email"):
                                value = meta.get(key)
                                if value:
                                    updates[key] = value
                            access_token = self._swap_access_token(access_token, updates)
        except Exception as exc:
            print(f"[refresh_accounts] source={source} swap failed: {exc}")

        return access_token

    def refresh_accounts(self, access_tokens: list[str]) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            return {"refreshed": 0, "errors": [], "items": self.list_accounts()}

        refreshed = 0
        errors = []
        max_workers = min(10, len(access_tokens))

        def _refresh_one(token: str) -> dict[str, Any] | None:
            current_token = self._refresh_one_by_source(token)
            return self.fetch_remote_info(current_token, "refresh_accounts")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_refresh_one, token): token
                for token in access_tokens
            }
            for future in as_completed(futures):
                try:
                    account = future.result()
                except Exception as exc:
                    errors.append({"token": anonymize_token(futures[future]), "error": str(exc)})
                    continue
                if account is not None:
                    refreshed += 1

        return {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
        }


    def get_stats(self) -> dict:
        with self._lock:
            items = list(self._accounts.values())
        total = len(items)
        active = sum(1 for a in items if a.get("status") == "正常")
        limited = sum(1 for a in items if a.get("status") == "限流")
        abnormal = sum(1 for a in items if a.get("status") == "异常")
        disabled = sum(1 for a in items if a.get("status") == "禁用")
        total_quota = sum(max(0, int(a.get("quota") or 0)) for a in items if a.get("status") == "正常")
        unlimited = sum(1 for a in items if a.get("status") == "正常" and bool(a.get("image_quota_unknown")))
        total_success = sum(int(a.get("success") or 0) for a in items)
        total_fail = sum(int(a.get("fail") or 0) for a in items)
        by_type = {}
        for a in items:
            t = a.get("type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total": total,
            "cumulative_total": self._cumulative_total,
            "active": active,
            "limited": limited,
            "abnormal": abnormal,
            "disabled": disabled,
            "total_quota": total_quota,
            "unlimited_quota_count": unlimited,
            "total_success": total_success,
            "total_fail": total_fail,
            "by_type": by_type,
        }

    def account_health(self) -> dict:
        stats = self.get_stats()
        return {
            "healthy": stats["active"] > 0 or stats["unlimited_quota_count"] > 0,
            "status": "ok" if stats["active"] > 0 else "degraded",
            **stats,
        }


account_service = AccountService(config.get_storage_backend())
