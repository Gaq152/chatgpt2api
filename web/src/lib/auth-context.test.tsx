import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth } from "@/lib/auth-context";
import { fetchValidatedSession, type ValidationResult } from "@/lib/auth-session";
import {
  clearStoredAuthSession,
  setStoredAuthSession,
  type StoredAuthSession,
} from "@/store/auth";

vi.mock("@/lib/auth-session", () => ({
  fetchValidatedSession: vi.fn(),
}));

vi.mock("@/store/auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/store/auth")>();
  return {
    ...actual,
    setStoredAuthSession: vi.fn().mockResolvedValue(undefined),
    clearStoredAuthSession: vi.fn().mockResolvedValue(undefined),
  };
});

const mockedFetch = vi.mocked(fetchValidatedSession);
const mockedSetStored = vi.mocked(setStoredAuthSession);
const mockedClearStored = vi.mocked(clearStoredAuthSession);

const adminSession: StoredAuthSession = {
  key: "k-admin",
  role: "admin",
  subjectId: "s-admin",
  name: "Alice",
  version: "v1",
};

const userSession: StoredAuthSession = {
  key: "k-user",
  role: "user",
  subjectId: "s-user",
  name: "Bob",
  version: "v1",
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function Consumer() {
  const { status, session, signIn, signOut, revalidate } = useAuth();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="role">{session?.role ?? "none"}</span>
      <button onClick={() => void signIn(userSession)}>signin</button>
      <button onClick={() => void signOut()}>signout</button>
      <button onClick={() => void revalidate()}>revalidate</button>
      <button onClick={() => void revalidate({ force: true })}>revalidate-force</button>
    </div>
  );
}

function renderProvider() {
  return render(
    <AuthProvider>
      <Consumer />
    </AuthProvider>,
  );
}

const status = () => screen.getByTestId("status").textContent;
const role = () => screen.getByTestId("role").textContent;

describe("AuthProvider", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("冷启动挂载时强制校验一次，成功后进入 authenticated", async () => {
    mockedFetch.mockResolvedValue({ kind: "ok", session: adminSession });

    renderProvider();

    await waitFor(() => expect(status()).toBe("authenticated"));
    expect(role()).toBe("admin");
    expect(mockedFetch).toHaveBeenCalledTimes(1);
    expect(mockedSetStored).toHaveBeenCalledWith(adminSession);
  });

  it("冷启动 none → unauthenticated 并清存储", async () => {
    mockedFetch.mockResolvedValue({ kind: "none" });

    renderProvider();

    await waitFor(() => expect(status()).toBe("unauthenticated"));
    expect(role()).toBe("none");
    expect(mockedClearStored).toHaveBeenCalled();
  });

  it("单飞去重：在途校验期间的多次 revalidate 只打一次后端", async () => {
    const d = deferred<ValidationResult>();
    mockedFetch.mockReturnValue(d.promise);

    renderProvider();
    // 冷启动已发起一次在途校验
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(1));

    // 在途期间再触发多次（含 force），都应复用同一在途请求
    fireEvent.click(screen.getByText("revalidate"));
    fireEvent.click(screen.getByText("revalidate-force"));
    fireEvent.click(screen.getByText("revalidate"));
    expect(mockedFetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      d.resolve({ kind: "ok", session: adminSession });
    });
    await waitFor(() => expect(status()).toBe("authenticated"));
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });

  it("generation 守卫：登出后，在途校验的成功结果被丢弃，不复活会话", async () => {
    const d = deferred<ValidationResult>();
    mockedFetch.mockReturnValue(d.promise);

    renderProvider();
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(1));

    // 在途校验未完成时登出
    await act(async () => {
      fireEvent.click(screen.getByText("signout"));
    });
    expect(status()).toBe("unauthenticated");

    // 在途校验此刻才返回「有效会话」——应被 generation 守卫丢弃
    await act(async () => {
      d.resolve({ kind: "ok", session: adminSession });
    });

    expect(status()).toBe("unauthenticated");
    expect(role()).toBe("none");
    // 关键：不得把会话写回存储（登出复活）
    expect(mockedSetStored).not.toHaveBeenCalled();
  });

  it("TTL 内的非强制 revalidate 跳过后端；force 则绕过 TTL", async () => {
    mockedFetch.mockResolvedValue({ kind: "ok", session: adminSession });

    renderProvider();
    await waitFor(() => expect(status()).toBe("authenticated"));
    expect(mockedFetch).toHaveBeenCalledTimes(1);

    // 刚校验过，TTL 内：非强制应跳过
    await act(async () => {
      fireEvent.click(screen.getByText("revalidate"));
    });
    expect(mockedFetch).toHaveBeenCalledTimes(1);

    // force 绕过 TTL
    await act(async () => {
      fireEvent.click(screen.getByText("revalidate-force"));
    });
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(2));
  });

  it("瞬时故障(transient)：冷启动保持 loading，不误判登出", async () => {
    mockedFetch.mockResolvedValue({ kind: "transient" });

    renderProvider();

    // 给足异步结算时间
    await act(async () => {
      await Promise.resolve();
    });
    expect(status()).toBe("loading");
    expect(mockedClearStored).not.toHaveBeenCalled();
  });

  it("瞬时故障(transient)：已登录用户保持 authenticated", async () => {
    mockedFetch.mockResolvedValueOnce({ kind: "ok", session: adminSession });

    renderProvider();
    await waitFor(() => expect(status()).toBe("authenticated"));

    mockedFetch.mockResolvedValueOnce({ kind: "transient" });
    await act(async () => {
      fireEvent.click(screen.getByText("revalidate-force"));
    });

    expect(status()).toBe("authenticated");
    expect(role()).toBe("admin");
  });

  it("invalid：已登录用户被登出并清存储", async () => {
    mockedFetch.mockResolvedValueOnce({ kind: "ok", session: adminSession });

    renderProvider();
    await waitFor(() => expect(status()).toBe("authenticated"));

    mockedFetch.mockResolvedValueOnce({ kind: "invalid" });
    await act(async () => {
      fireEvent.click(screen.getByText("revalidate-force"));
    });

    await waitFor(() => expect(status()).toBe("unauthenticated"));
    expect(mockedClearStored).toHaveBeenCalled();
  });

  it("signIn 立即建立可信会话，signOut 清除", async () => {
    mockedFetch.mockResolvedValue({ kind: "none" });

    renderProvider();
    await waitFor(() => expect(status()).toBe("unauthenticated"));

    await act(async () => {
      fireEvent.click(screen.getByText("signin"));
    });
    expect(status()).toBe("authenticated");
    expect(role()).toBe("user");
    expect(mockedSetStored).toHaveBeenCalledWith(userSession);

    await act(async () => {
      fireEvent.click(screen.getByText("signout"));
    });
    expect(status()).toBe("unauthenticated");
    expect(role()).toBe("none");
  });
});
