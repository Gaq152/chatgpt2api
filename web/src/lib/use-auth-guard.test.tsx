import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAuthGuard, useRedirectIfAuthenticated } from "@/lib/use-auth-guard";
import { useAuth } from "@/lib/auth-context";
import type { AuthStatus } from "@/lib/auth-context";
import type { StoredAuthSession } from "@/store/auth";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

const mockedUseAuth = vi.mocked(useAuth);

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

function setAuth(status: AuthStatus, session: StoredAuthSession | null) {
  mockedUseAuth.mockReturnValue({
    status,
    session,
    signIn: vi.fn(),
    signOut: vi.fn(),
    revalidate: vi.fn(),
  });
}

function GuardProbe({ allowedRoles }: { allowedRoles?: ("admin" | "user")[] }) {
  const { isCheckingAuth, session } = useAuthGuard(allowedRoles);
  return (
    <div>
      <span data-testid="checking">{String(isCheckingAuth)}</span>
      <span data-testid="role">{session?.role ?? "none"}</span>
    </div>
  );
}

function RedirectProbe() {
  const { isCheckingAuth } = useRedirectIfAuthenticated();
  return <span data-testid="checking">{String(isCheckingAuth)}</span>;
}

describe("useAuthGuard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loading：isCheckingAuth=true，不发生任何跳转", async () => {
    setAuth("loading", null);

    render(<GuardProbe allowedRoles={["admin"]} />);

    expect(screen.getByTestId("checking").textContent).toBe("true");
    // 等待一次微任务，确保 effect 没有误跳
    await waitFor(() => expect(replace).not.toHaveBeenCalled());
  });

  it("未登录：重定向到 /login", async () => {
    setAuth("unauthenticated", null);

    render(<GuardProbe allowedRoles={["admin"]} />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });

  it("越权（user 访问 admin 页）：重定向到角色默认路由 /image", async () => {
    setAuth("authenticated", userSession);

    render(<GuardProbe allowedRoles={["admin"]} />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/image"));
  });

  it("admin 访问 admin 页：放行，不跳转", async () => {
    setAuth("authenticated", adminSession);

    render(<GuardProbe allowedRoles={["admin"]} />);

    await waitFor(() => expect(screen.getByTestId("role").textContent).toBe("admin"));
    expect(replace).not.toHaveBeenCalled();
  });

  it("无 allowedRoles：任意已登录角色放行", async () => {
    setAuth("authenticated", userSession);

    render(<GuardProbe />);

    await waitFor(() => expect(screen.getByTestId("role").textContent).toBe("user"));
    expect(replace).not.toHaveBeenCalled();
  });

  it("authenticated 时返回的 session 反映可信会话", () => {
    setAuth("authenticated", adminSession);

    render(<GuardProbe allowedRoles={["admin"]} />);

    expect(screen.getByTestId("checking").textContent).toBe("false");
    expect(screen.getByTestId("role").textContent).toBe("admin");
  });
});

describe("useRedirectIfAuthenticated", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("已登录：跳转到角色默认路由（admin → /accounts）", async () => {
    setAuth("authenticated", adminSession);

    render(<RedirectProbe />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/accounts"));
    // 已登录视为仍在 checking（避免登录表单一闪）
    expect(screen.getByTestId("checking").textContent).toBe("true");
  });

  it("未登录：不跳转，结束 checking 以展示登录表单", async () => {
    setAuth("unauthenticated", null);

    render(<RedirectProbe />);

    expect(screen.getByTestId("checking").textContent).toBe("false");
    await waitFor(() => expect(replace).not.toHaveBeenCalled());
  });

  it("loading：保持 checking", () => {
    setAuth("loading", null);

    render(<RedirectProbe />);

    expect(screen.getByTestId("checking").textContent).toBe("true");
  });
});
