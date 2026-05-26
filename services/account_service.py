from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Condition, Event, Lock
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
        self._refresh_events: dict[str, Event] = {}
        self._refresh_results: dict[str, str] = {}
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
        return datetime.now(timezone.utc).isoformat()

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

    @staticmethod
    def _account_identity(item: dict) -> tuple[str, str]:
        """按 email > user_id 优先级返回账号身份标识。

        返回 (kind, value)，kind 为 "email" / "user_id" / ""（无可用标识）。
        用于导入去重：源端轮换 access_token 后再次导入，仍要识别为同一条账户。
        """
        email = str((item or {}).get("email") or "").strip()
        if email:
            return ("email", email.lower())
        user_id = str((item or {}).get("user_id") or "").strip()
        if user_id:
            return ("user_id", user_id)
        return ("", "")

    def _find_existing_token_locked(self, identity: tuple[str, str]) -> str:
        """在 _accounts 里按身份找已存在条目的 access_token，找不到返回 ""。调用方需已持锁。"""
        kind, value = identity
        if not kind or not value:
            return ""
        for token, account in self._accounts.items():
            existing = self._account_identity(account)
            if existing == identity:
                return token
        return ""

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

        # email / user_id 是账号身份标识，导入时优先从 incoming 取，缺失时尝试从 access_token JWT 解。
        # 让"按 email/user_id 去重"在所有场景（注册 / sub2api / cpa / manual）都能找到稳定标识。
        email = normalized.get("email") or None
        user_id = normalized.get("user_id") or None
        if not email or not user_id:
            access_payload = AccountService._decode_jwt_payload(access_token)
            if isinstance(access_payload, dict):
                if not email:
                    profile = access_payload.get("https://api.openai.com/profile")
                    if isinstance(profile, dict):
                        email = str(profile.get("email") or "").strip() or None
                if not user_id:
                    auth_claim = access_payload.get("https://api.openai.com/auth")
                    if isinstance(auth_claim, dict):
                        user_id = str(auth_claim.get("user_id") or "").strip() or None
                    if not user_id:
                        user_id = str(access_payload.get("sub") or "").strip() or None
        normalized["email"] = email
        normalized["user_id"] = user_id
        limits_progress = normalized.get("limits_progress")
        normalized["limits_progress"] = limits_progress if isinstance(limits_progress, list) else []
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        normalized["created_at"] = normalized.get("created_at") or AccountService._now()
        normalized["invalid_count"] = int(normalized.get("invalid_count") or 0)
        normalized["last_invalid_at"] = normalized.get("last_invalid_at") or None
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
        mailbox_data = normalized.get("mailbox_data")
        normalized["mailbox_data"] = mailbox_data if isinstance(mailbox_data, dict) else None
        normalized["status_reason"] = (normalized.get("status_reason") or "") or None
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
            next_item["last_used_at"] = self._now()
            # 调用成功 → 清 401 计数，下次再撞 401 重新走缓判窗口
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._save_accounts()

    # 401 误判保护参数：新号缓冲 + 重复确认窗口
    _NEW_ACCOUNT_INVALID_GRACE_SECONDS = 10 * 60  # 新号 10 分钟内首次 401 不删
    _INVALID_CONFIRM_SECONDS = 30                 # 30 秒内重复 401 视为同一次抖动

    @staticmethod
    def _parse_invalid_time(value: object) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _should_defer_invalid(self, account: dict, now: datetime) -> bool:
        """新号宽限 / 第一次 401 / 30 秒内重复 401 都缓判，连续两次确认才允许真删。"""
        created_at = self._parse_invalid_time(account.get("created_at"))
        if created_at and (now - created_at).total_seconds() < self._NEW_ACCOUNT_INVALID_GRACE_SECONDS:
            return True
        invalid_count = int(account.get("invalid_count") or 0)
        if invalid_count <= 1:
            return True
        last_invalid_at = self._parse_invalid_time(account.get("last_invalid_at"))
        if last_invalid_at and (now - last_invalid_at).total_seconds() < self._INVALID_CONFIRM_SECONDS:
            return True
        return False

    def _record_invalid_seen(self, access_token: str, event: str) -> bool:
        """记一次 401，返回是否允许真删。

        返回 True：累计次数和时间窗满足真删条件，调用方继续走 remove。
        返回 False：缓判，写入计数和时间但不删；下次还撞 401 才会被允许真删。
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return True  # 已经不在号池，调用方按删处理无副作用
            should_defer = self._should_defer_invalid(current, now)
            next_item = dict(current)
            next_item["invalid_count"] = int(next_item.get("invalid_count") or 0) + 1
            next_item["last_invalid_at"] = now.isoformat()
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account
                self._save_accounts()
        if should_defer:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "暂缓标记异常账号",
                {"source": event, "token": anonymize_token(access_token),
                 "invalid_count": int((account or current).get("invalid_count") or 0)},
            )
        return not should_defer

    def _clear_invalid_count(self, access_token: str) -> None:
        """token 调用 / 刷新成功后清零计数，下次再撞 401 重新走缓判窗口。"""
        if not access_token:
            return
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return
            if not int(current.get("invalid_count") or 0) and not current.get("last_invalid_at"):
                return
            next_item = dict(current)
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account
                self._save_accounts()

    def remove_invalid_token(self, access_token: str, event: str, reason: str = "") -> bool:
        # 缓判：新号 10 分钟内 / 第一次 401 / 30 秒内重复 401 都先记账不真删，
        # 连续两次确认（且超出抖动窗口）才放过来走原删除流程。
        if access_token and not self._record_invalid_seen(access_token, event):
            return False
        status_reason = reason or f"token 无效 ({event})"
        if not config.auto_remove_invalid_accounts:
            self.update_account(access_token, {"status": "异常", "quota": 0, "status_reason": status_reason})
            return False
        removed = bool(self.delete_accounts([access_token])["removed"])
        if removed:
            log_service.add(LOG_TYPE_ACCOUNT, "自动移除异常账号",
                            {"source": event, "token": anonymize_token(access_token)})
        elif access_token:
            self.update_account(access_token, {"status": "异常", "quota": 0, "status_reason": status_reason})
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
            result: list[dict] = []
            for item in self._accounts.values():
                row = {key: value for key, value in item.items() if key not in {"password", "mailbox_data"}}
                mailbox_data = item.get("mailbox_data")
                if isinstance(mailbox_data, dict) and mailbox_data.get("provider"):
                    row["mail_provider"] = mailbox_data["provider"]
                result.append(row)
            return result

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

    def list_refreshable_tokens(self) -> list[str]:
        """watcher 定时刷新的候选：除禁用外的全部账号。

        异常 / 限流 / 正常都会被刷一次，因为按 source 分流后：
        - manual/register 走 OAuth refresh_token 刷三件套
        - sub2api/cpa 走平台重拉
        - 这些动作本身就能恢复异常号、回查限流号、续期正常号
        """
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") != "禁用"
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
        "mailbox_data",
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
            seen_tokens: set[str] = set()
            seen_identities: set[tuple[str, str]] = set()
            for incoming in cleaned:
                access_token = incoming["access_token"]
                if access_token in seen_tokens:
                    continue

                # 入库前先用 access_token JWT 解出 email / user_id（如果 incoming 没带），
                # 这样 _account_identity 才有稳定标识。这一步顺手把字段补回 incoming，
                # 后续合并/写盘也能保留下来。
                preview = self._normalize_account({**incoming})
                if preview is None:
                    continue
                identity = self._account_identity(preview)
                if identity[0]:
                    if identity in seen_identities:
                        continue
                    seen_identities.add(identity)
                    incoming.setdefault("email", preview.get("email"))
                    incoming.setdefault("user_id", preview.get("user_id"))

                # 优先按身份找已存在条目。源端轮换 access_token 后再次导入，
                # 仍能识别为同一账号 → 删旧 key、用新 access_token 入库，避免产生重复条目。
                existing_token = self._find_existing_token_locked(identity) if identity[0] else ""
                if existing_token and existing_token != access_token:
                    base = dict(self._accounts[existing_token])
                    self._accounts.pop(existing_token, None)
                    inflight = self._image_inflight.pop(existing_token, None)
                    if inflight:
                        self._image_inflight[access_token] = inflight
                    skipped += 1
                elif existing_token == access_token:
                    base = dict(self._accounts[access_token])
                    skipped += 1
                else:
                    current = self._accounts.get(access_token)
                    if current is None:
                        added += 1
                        self._cumulative_total += 1
                        self._save_cumulative_total()
                        base = {"created_at": self._now()}
                    else:
                        base = dict(current)
                        skipped += 1

                seen_tokens.add(access_token)
                merged = {**base, **incoming, "access_token": access_token}
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
            merged = {**current, **updates, "access_token": access_token}
            if merged.get("status") not in {"异常"} and "status_reason" not in updates:
                merged["status_reason"] = None
            account = self._normalize_account(merged)
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
            next_item["last_used_at"] = self._now()
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
                # 调用成功 → 清 401 计数
                next_item["invalid_count"] = 0
                next_item["last_invalid_at"] = None
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
        except InvalidAccessTokenError as exc:
            self.remove_invalid_token(access_token, event, reason=str(exc))
            raise
        # 远端拿到信息说明 token 仍然有效，清掉之前累计的 401 计数
        self._clear_invalid_count(access_token)
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
            # 刷新成功 → 清 401 计数，新 token 视为干净起点
            merged = {**current, **updates, "access_token": new_token,
                      "invalid_count": 0, "last_invalid_at": None}
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

        OpenAI 使用 rotating refresh token——用过一次即作废。如果多个线程同时拿同一个
        refresh_token 去刷新，第一个成功、后续全部 "already been used"。这里用
        per-token Event 做并发屏障：同一 token 同时只有一个线程执行刷新，其余等待结果。
        """
        with self._lock:
            existing_event = self._refresh_events.get(access_token)
            if existing_event is not None:
                wait_event = existing_event
            else:
                account = self._accounts.get(access_token)
                snapshot = dict(account) if account else None
                if snapshot is None:
                    return access_token
                wait_event = None
                event = Event()
                self._refresh_events[access_token] = event

        if wait_event is not None:
            wait_event.wait(timeout=120)
            return self._refresh_results.get(access_token, access_token)

        result_token = access_token
        try:
            result_token = self._do_refresh(access_token, snapshot)
        finally:
            with self._lock:
                self._refresh_events.pop(access_token, None)
                self._refresh_results[access_token] = result_token
            event.set()

        return result_token

    def _try_relogin_recovery(self, access_token: str, snapshot: dict) -> str:
        """refresh_token 作废后，尝试用存储的邮箱+密码重新登录换取全新三件套。"""
        email = str(snapshot.get("email") or "").strip()
        password = str(snapshot.get("password") or "").strip()
        if not email or not password:
            return access_token
        try:
            from services.register.openai_register import relogin_for_tokens
            print(f"[refresh_accounts] refresh_token 失效，尝试密码重登: {email}")
            mailbox_data = snapshot.get("mailbox_data") if isinstance(snapshot.get("mailbox_data"), dict) else None
            login_tokens = relogin_for_tokens(email, password, mailbox_data)
            if login_tokens and login_tokens.get("access_token"):
                updates: dict[str, Any] = {"access_token": login_tokens["access_token"]}
                if login_tokens.get("refresh_token"):
                    updates["refresh_token"] = login_tokens["refresh_token"]
                if login_tokens.get("id_token"):
                    updates["id_token"] = login_tokens["id_token"]
                return self._swap_access_token(access_token, updates)
        except Exception as exc:
            print(f"[refresh_accounts] 密码重登异常: {email}: {exc}")
        return access_token

    def _do_refresh(self, access_token: str, snapshot: dict) -> str:
        source = str(snapshot.get("source") or "manual")
        try:
            if source in {"manual", "register"}:
                refresh_token = str(snapshot.get("refresh_token") or "").strip()
                refreshed = False
                if refresh_token:
                    try:
                        from services.register.openai_register import refresh_platform_tokens
                        new_tokens = refresh_platform_tokens(refresh_token, access_token)
                        if new_tokens and new_tokens.get("access_token"):
                            updates: dict[str, Any] = {"access_token": new_tokens["access_token"]}
                            if new_tokens.get("refresh_token"):
                                updates["refresh_token"] = new_tokens["refresh_token"]
                            if new_tokens.get("id_token"):
                                updates["id_token"] = new_tokens["id_token"]
                            access_token = self._swap_access_token(access_token, updates)
                            refreshed = True
                    except Exception as exc:
                        print(f"[refresh_accounts] refresh_token 刷新失败: {exc}")
                if not refreshed:
                    access_token = self._try_relogin_recovery(access_token, snapshot)
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
                    or str(snapshot.get("email") or "").strip()
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

    def try_refresh_access_token(self, access_token: str) -> str:
        """对外公开的 token 刷新入口，按 source 分流，返回当前生效的 access_token。

        刷不出来或刷不动时返回原 token。流式 401 兜底场景调用，避免直接暴露 _refresh_one_by_source。
        """
        if not access_token:
            return access_token
        return self._refresh_one_by_source(access_token)

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
