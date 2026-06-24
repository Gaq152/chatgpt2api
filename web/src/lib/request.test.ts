import { describe, expect, it, vi } from "vitest";

const { mockInstance } = vi.hoisted(() => ({
  mockInstance: {
    request: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  },
}));

vi.mock("axios", () => ({
  default: { create: () => mockInstance },
  AxiosError: class AxiosError extends Error {},
}));

// 必须在 mock 之后导入：模块求值时会调用 axios.create 并注册响应拦截器
import { httpRequest } from "@/lib/request";

// 取出 request.ts 注册的响应错误拦截器（retry/cancel 逻辑所在）
function getOnRejected() {
  const call = mockInstance.interceptors.response.use.mock.calls[0];
  return call[1] as (error: unknown) => Promise<unknown>;
}

describe("request 重试拦截器", () => {
  it("主动取消(ERR_CANCELED)不重试，直接 reject", async () => {
    const onRejected = getOnRejected();
    mockInstance.request.mockClear();

    const error = {
      code: "ERR_CANCELED",
      message: "canceled",
      config: { retry: 2, __retryCount: 0, retryDelay: 0 },
    };

    await expect(onRejected(error)).rejects.toBeInstanceOf(Error);
    expect(mockInstance.request).not.toHaveBeenCalled();
  });

  it("5xx 瞬时故障会重试", async () => {
    const onRejected = getOnRejected();
    mockInstance.request.mockClear();
    mockInstance.request.mockResolvedValue({ data: "ok" });

    const error = {
      message: "server error",
      response: { status: 503 },
      config: { retry: 2, __retryCount: 0, retryDelay: 0 },
    };

    await onRejected(error);
    expect(mockInstance.request).toHaveBeenCalledTimes(1);
  });

  it("网络错误(无 response)会重试", async () => {
    const onRejected = getOnRejected();
    mockInstance.request.mockClear();
    mockInstance.request.mockResolvedValue({ data: "ok" });

    const error = {
      code: "ERR_NETWORK",
      message: "Network Error",
      config: { retry: 2, __retryCount: 0, retryDelay: 0 },
    };

    await onRejected(error);
    expect(mockInstance.request).toHaveBeenCalledTimes(1);
  });
});

describe("httpRequest 透传 signal", () => {
  it("将 AbortSignal 透传到 axios config", async () => {
    mockInstance.request.mockClear();
    mockInstance.request.mockResolvedValue({ data: { ok: true } });

    const controller = new AbortController();
    const result = await httpRequest("/x", { signal: controller.signal });

    expect(mockInstance.request).toHaveBeenCalledWith(
      expect.objectContaining({ signal: controller.signal }),
    );
    expect(result).toEqual({ ok: true });
  });
});
