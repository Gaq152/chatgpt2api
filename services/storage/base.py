from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    """抽象存储后端基类"""

    @abstractmethod
    def load_accounts(self) -> list[dict[str, Any]]:
        """加载所有账号数据"""
        pass

    @abstractmethod
    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存所有账号数据"""
        pass

    @abstractmethod
    def load_auth_keys(self) -> list[dict[str, Any]]:
        """加载所有鉴权密钥数据"""
        pass

    @abstractmethod
    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存所有鉴权密钥数据"""
        pass

    def load_blocked_domains(self) -> list[dict[str, Any]]:
        """加载被封禁的邮箱域名列表"""
        return []

    def save_blocked_domains(self, domains: list[dict[str, Any]]) -> None:
        """保存被封禁的邮箱域名列表"""
        pass

    def load_image_conversations(self) -> list[dict[str, Any]]:
        """加载图片会话数据"""
        return []

    def save_image_conversations(self, conversations: list[dict[str, Any]]) -> None:
        """保存图片会话数据"""
        pass

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """健康检查，返回存储后端状态"""
        pass

    @abstractmethod
    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        pass
