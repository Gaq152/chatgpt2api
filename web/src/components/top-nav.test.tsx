import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TopNav } from "@/components/top-nav";
import { useAuth } from "@/lib/auth-context";
import type { AuthStatus } from "@/lib/auth-context";
import type { StoredAuthSession } from "@/store/auth";

const replace = vi.fn();
const signOut = vi.fn().mockResolvedValue(undefined);
let pathname = "/image";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => pathname,
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

// ThemeToggle 依赖主题环境，测试中替换为占位，专注导航逻辑
vi.mock("@/components/theme-toggle", () => ({
  ThemeToggle: () => <div data-testid="theme-toggle" />,
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
    signOut,
    revalidate: vi.fn(),
  });
}

const ADMIN_LABELS = ["画图", "号池管理", "注册机", "图片管理", "日志管理", "设置"];

describe("TopNav", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    pathname = "/image";
  });

  it("admin 渲染全部 6 个导航项", () => {
    setAuth("authenticated", adminSession);

    render(<TopNav />);

    for (const label of ADMIN_LABELS) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("user 仅渲染「画图」，看不到任何 admin 导航项", () => {
    setAuth("authenticated", userSession);

    render(<TopNav />);

    expect(screen.getByRole("link", { name: "画图" })).toBeInTheDocument();
    for (const label of ["号池管理", "注册机", "图片管理", "日志管理", "设置"]) {
      expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
  });

  it("/login 路径不渲染导航", () => {
    pathname = "/login";
    setAuth("authenticated", adminSession);

    const { container } = render(<TopNav />);

    expect(container).toBeEmptyDOMElement();
  });

  it("未登录不渲染导航", () => {
    setAuth("unauthenticated", null);

    const { container } = render(<TopNav />);

    expect(container).toBeEmptyDOMElement();
  });

  it("loading 状态不渲染导航（避免无会话时闪现）", () => {
    setAuth("loading", null);

    const { container } = render(<TopNav />);

    expect(container).toBeEmptyDOMElement();
  });

  it("点击退出：调用 signOut 后跳转 /login", async () => {
    setAuth("authenticated", adminSession);

    render(<TopNav />);

    fireEvent.click(screen.getAllByText("退出")[0]);

    await waitFor(() => expect(signOut).toHaveBeenCalledTimes(1));
    expect(replace).toHaveBeenCalledWith("/login");
  });
});
