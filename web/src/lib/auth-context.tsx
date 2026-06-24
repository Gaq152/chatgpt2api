"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { fetchValidatedSession } from "@/lib/auth-session";
import {
  clearStoredAuthSession,
  setStoredAuthSession,
  type StoredAuthSession,
} from "@/store/auth";

// 后台静默刷新的 TTL：距上次成功校验超过此值，标签页重新可见 / 定时器才会再打后端。
// 导航本身永不触发校验 —— 这是「切页不卡」的关键。
const REVALIDATE_TTL = 5 * 60_000;
const REVALIDATE_INTERVAL = 5 * 60_000;

export type AuthStatus = "loading" | "authenticated" | "unauthenticated";

type AuthContextValue = {
  status: AuthStatus;
  session: StoredAuthSession | null;
  // 登录成功后由登录页调用，写入可信会话
  signIn: (session: StoredAuthSession) => Promise<void>;
  // 登出：清存储并置为未登录
  signOut: () => Promise<void>;
  // 触发一次后台校验（force 时忽略 TTL）
  revalidate: (options?: { force?: boolean }) => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [session, setSession] = useState<StoredAuthSession | null>(null);

  // 代次：signIn/signOut 时自增；在途校验提交前比对，不一致则丢弃 —— 根治登出复活竞态
  const generationRef = useRef(0);
  // 单飞：所有触发点复用同一次在途校验 —— 根治 force 并发重复打后端
  const inflightRef = useRef<Promise<void> | null>(null);
  // 上次成功校验时间，用于 TTL 判定
  const lastValidatedAtRef = useRef(0);

  const runValidation = useCallback(async () => {
    const generationAtStart = generationRef.current;
    const result = await fetchValidatedSession();

    // 在途期间发生了 signIn/signOut（代次变化）：本次结果作废，避免覆盖更新的状态
    if (generationAtStart !== generationRef.current) {
      return;
    }

    switch (result.kind) {
      case "ok":
        await setStoredAuthSession(result.session);
        // 持久化后再次确认代次未变，防止 await 期间被登出
        if (generationAtStart !== generationRef.current) {
          return;
        }
        lastValidatedAtRef.current = Date.now();
        setSession(result.session);
        setStatus("authenticated");
        break;
      case "none":
      case "invalid":
        await clearStoredAuthSession();
        if (generationAtStart !== generationRef.current) {
          return;
        }
        setSession(null);
        setStatus("unauthenticated");
        break;
      case "transient":
        // 瞬时故障：保留现有会话与状态。冷启动（仍是 loading）则维持 loading，
        // 由定时器 / 标签页可见时的下次校验继续尝试，不误判登出。
        break;
    }
  }, []);

  const revalidate = useCallback(
    async (options?: { force?: boolean }) => {
      const force = options?.force ?? false;
      // 任何在途校验都复用（force 也复用）：单飞，避免并发重复打后端
      if (inflightRef.current) {
        return inflightRef.current;
      }
      // 非强制且距上次校验未超 TTL：跳过，不打后端
      if (!force && Date.now() - lastValidatedAtRef.current < REVALIDATE_TTL) {
        return;
      }
      const task = runValidation().finally(() => {
        inflightRef.current = null;
      });
      inflightRef.current = task;
      return task;
    },
    [runValidation],
  );

  const signIn = useCallback(async (next: StoredAuthSession) => {
    generationRef.current += 1;
    inflightRef.current = null;
    await setStoredAuthSession(next);
    lastValidatedAtRef.current = Date.now();
    setSession(next);
    setStatus("authenticated");
  }, []);

  const signOut = useCallback(async () => {
    generationRef.current += 1;
    inflightRef.current = null;
    lastValidatedAtRef.current = 0;
    await clearStoredAuthSession();
    setSession(null);
    setStatus("unauthenticated");
  }, []);

  // 冷启动：挂载时强制校验一次，建立初始会话状态
  useEffect(() => {
    void revalidate({ force: true });
  }, [revalidate]);

  // 标签页重新可见：距上次校验超过 TTL 才后台刷新（不阻塞任何导航）
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState !== "visible") {
        return;
      }
      void revalidate();
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [revalidate]);

  // 定时后台刷新：仅在页面可见时尝试，捕获后端禁用 key / 改 role / 升级版本
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") {
        void revalidate();
      }
    }, REVALIDATE_INTERVAL);
    return () => {
      clearInterval(timer);
    };
  }, [revalidate]);

  return (
    <AuthContext.Provider value={{ status, session, signIn, signOut, revalidate }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth 必须在 AuthProvider 内部使用");
  }
  return context;
}
