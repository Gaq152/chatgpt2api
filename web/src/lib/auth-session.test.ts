import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchValidatedSession } from "@/lib/auth-session";
import { login } from "@/lib/api";
import { getStoredAuthSession, type StoredAuthSession } from "@/store/auth";

vi.mock("@/lib/api", () => ({
  login: vi.fn(),
}));

vi.mock("@/store/auth", () => ({
  getStoredAuthSession: vi.fn(),
}));

const mockedLogin = vi.mocked(login);
const mockedGetStored = vi.mocked(getStoredAuthSession);

const storedSession: StoredAuthSession = {
  key: "k-1",
  role: "admin",
  subjectId: "s-1",
  name: "Alice",
  version: "v1",
};

function loginResponse(overrides?: Partial<Awaited<ReturnType<typeof login>>>) {
  return {
    ok: true,
    version: "v1",
    role: "admin" as const,
    subject_id: "s-1",
    name: "Alice",
    ...overrides,
  };
}

describe("fetchValidatedSession", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("本地无 key 时返回 none，不打后端", async () => {
    mockedGetStored.mockResolvedValue(null);

    const result = await fetchValidatedSession();

    expect(result).toEqual({ kind: "none" });
    expect(mockedLogin).not.toHaveBeenCalled();
  });

  it("校验成功返回 ok，并以后端 role/name 为准", async () => {
    mockedGetStored.mockResolvedValue(storedSession);
    mockedLogin.mockResolvedValue(loginResponse({ role: "user", name: "Bob" }));

    const result = await fetchValidatedSession();

    expect(result.kind).toBe("ok");
    if (result.kind === "ok") {
      expect(result.session).toEqual({
        key: "k-1",
        role: "user",
        subjectId: "s-1",
        name: "Bob",
        version: "v1",
      });
    }
  });

  it("version 不符判定为 invalid（后端已更新/重启）", async () => {
    mockedGetStored.mockResolvedValue(storedSession);
    mockedLogin.mockResolvedValue(loginResponse({ version: "v2" }));

    const result = await fetchValidatedSession();

    expect(result).toEqual({ kind: "invalid" });
  });

  it("401 判定为 invalid（确切失效）", async () => {
    mockedGetStored.mockResolvedValue(storedSession);
    const error = new Error("密钥失效") as Error & { status?: number };
    error.status = 401;
    mockedLogin.mockRejectedValue(error);

    const result = await fetchValidatedSession();

    expect(result).toEqual({ kind: "invalid" });
  });

  it("403 判定为 invalid", async () => {
    mockedGetStored.mockResolvedValue(storedSession);
    const error = new Error("无权限") as Error & { status?: number };
    error.status = 403;
    mockedLogin.mockRejectedValue(error);

    const result = await fetchValidatedSession();

    expect(result).toEqual({ kind: "invalid" });
  });

  it("网络错误（无 status）判定为 transient，不误判登出", async () => {
    mockedGetStored.mockResolvedValue(storedSession);
    mockedLogin.mockRejectedValue(new Error("Network Error"));

    const result = await fetchValidatedSession();

    expect(result).toEqual({ kind: "transient" });
  });

  it("5xx 判定为 transient", async () => {
    mockedGetStored.mockResolvedValue(storedSession);
    const error = new Error("服务器错误") as Error & { status?: number };
    error.status = 503;
    mockedLogin.mockRejectedValue(error);

    const result = await fetchValidatedSession();

    expect(result).toEqual({ kind: "transient" });
  });
});
