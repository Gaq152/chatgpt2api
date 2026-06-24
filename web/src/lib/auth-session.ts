"use client";

import { login } from "@/lib/api";
import { getStoredAuthSession, type StoredAuthSession } from "@/store/auth";

// 会话校验的判定结果。持久化/清除存储与状态提交全部交由 AuthProvider
// 按 generation 守卫执行，这里只做「读 + 打后端 + 判定」，保持纯粹、无副作用残留。
export type ValidationResult =
  | { kind: "ok"; session: StoredAuthSession }
  // 本地无 key（未登录）
  | { kind: "none" }
  // 后端明确判定失效：401 或 version 不符 —— 应登出
  | { kind: "invalid" }
  // 网络/超时/5xx 等瞬时故障（已在 request 层重试仍失败）—— 不应误判登出
  | { kind: "transient" };

// 读取本地 key，向后端换取并校验会话信息。不写任何存储、不改任何缓存。
export async function fetchValidatedSession(): Promise<ValidationResult> {
  const storedSession = await getStoredAuthSession();
  if (!storedSession) {
    return { kind: "none" };
  }

  try {
    const data = await login(storedSession.key);
    // version 不符：后端已重启/更新，旧会话作废
    if (data.version && storedSession.version !== data.version) {
      return { kind: "invalid" };
    }
    return {
      kind: "ok",
      session: {
        key: storedSession.key,
        role: data.role,
        subjectId: data.subject_id,
        name: data.name,
        version: data.version,
      },
    };
  } catch (error) {
    // 仅 401 视为确切失效；网络/超时/5xx 视为瞬时故障，保留现有会话
    const status = (error as { status?: number } | null)?.status;
    if (status === 401 || status === 403) {
      return { kind: "invalid" };
    }
    return { kind: "transient" };
  }
}
