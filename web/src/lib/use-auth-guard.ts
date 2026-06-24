"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "@/lib/auth-context";
import {
  getDefaultRouteForRole,
  type AuthRole,
  type StoredAuthSession,
} from "@/store/auth";

type UseAuthGuardResult = {
  isCheckingAuth: boolean;
  session: StoredAuthSession | null;
};

export function useAuthGuard(allowedRoles?: AuthRole[]): UseAuthGuardResult {
  const router = useRouter();
  const { status, session } = useAuth();
  const allowedRolesKey = (allowedRoles || []).join(",");

  useEffect(() => {
    // 仍在建立会话：等结果，不做任何跳转
    if (status === "loading") {
      return;
    }

    if (status === "unauthenticated" || !session) {
      router.replace("/login");
      return;
    }

    // 基于后端确认过的 role 做权限决策（本地存储被篡改不影响这里）。
    // SPA 切页时 Context 已同步持有 role，越权用户在页面内容渲染前即被重定向。
    const roleList = allowedRolesKey ? (allowedRolesKey.split(",") as AuthRole[]) : [];
    if (roleList.length > 0 && !roleList.includes(session.role)) {
      router.replace(getDefaultRouteForRole(session.role));
    }
  }, [status, session, allowedRolesKey, router]);

  return {
    isCheckingAuth: status === "loading",
    session: status === "authenticated" ? session : null,
  };
}

export function useRedirectIfAuthenticated() {
  const router = useRouter();
  const { status, session } = useAuth();

  useEffect(() => {
    if (status === "authenticated" && session) {
      router.replace(getDefaultRouteForRole(session.role));
    }
  }, [status, session, router]);

  return { isCheckingAuth: status === "loading" || status === "authenticated" };
}
