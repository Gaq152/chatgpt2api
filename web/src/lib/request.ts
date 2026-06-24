import axios, {AxiosError, type AxiosRequestConfig} from "axios";

import webConfig from "@/constants/common-env";
import {clearStoredAuthSession, getStoredAuthKey} from "@/store/auth";

type RequestConfig = AxiosRequestConfig & {
    redirectOnUnauthorized?: boolean;
    retry?: number;
    retryDelay?: number;
    __retryCount?: number;
};

const DEFAULT_TIMEOUT_MS = 15_000;

type ErrorPayload = {
    detail?: string | { error?: string | { message?: string } };
    error?: string | { message?: string };
    message?: string;
};

function errorMessageFromValue(value: unknown): string {
    if (typeof value === "string") {
        return value;
    }
    if (!value || typeof value !== "object") {
        return "";
    }

    const item = value as { error?: unknown; message?: unknown };
    if (typeof item.message === "string") {
        return item.message;
    }
    return errorMessageFromValue(item.error);
}

export const request = axios.create({
    baseURL: webConfig.apiUrl.replace(/\/$/, ""),
    timeout: DEFAULT_TIMEOUT_MS,
});

request.interceptors.request.use(async (config) => {
    const nextConfig = {...config};
    const authKey = await getStoredAuthKey();
    const headers = {...(nextConfig.headers || {})} as Record<string, string>;
    if (authKey && !headers.Authorization) {
        headers.Authorization = `Bearer ${authKey}`;
    }
    // eslint-disable-next-line @typescript-eslint/ban-ts-comment
    // @ts-expect-error
    nextConfig.headers = headers;
    return nextConfig;
});

request.interceptors.response.use(
    (response) => response,
    async (error: AxiosError<ErrorPayload>) => {
        const status = error.response?.status;
        const requestConfig = error.config as RequestConfig | undefined;
        const shouldRedirect = requestConfig?.redirectOnUnauthorized !== false;
        if (status === 401 && shouldRedirect && typeof window !== "undefined") {
            // Avoid redirect loop — only redirect if not already on /login
            if (!window.location.pathname.startsWith("/login")) {
                await clearStoredAuthSession();
                window.location.replace("/login");
                // Return a never-resolving promise to prevent further error handling
                // while the browser navigates away
                return new Promise(() => {});
            }
        }

        // 仅对网络错误、超时、5xx 这类瞬时故障做有限次重试，避免偶发抖动直接失败
        if (requestConfig) {
            const maxRetry = requestConfig.retry ?? 0;
            const retriedCount = requestConfig.__retryCount ?? 0;
            const isRetriable =
                !error.response ||
                error.code === "ECONNABORTED" ||
                error.code === "ETIMEDOUT" ||
                error.code === "ERR_NETWORK" ||
                (typeof status === "number" && status >= 500 && status <= 599);
            if (maxRetry > 0 && retriedCount < maxRetry && isRetriable) {
                requestConfig.__retryCount = retriedCount + 1;
                const baseDelay = requestConfig.retryDelay ?? 500;
                const delay = baseDelay * 2 ** retriedCount;
                await new Promise((resolve) => setTimeout(resolve, delay));
                return request.request(requestConfig);
            }
        }

        const payload = error.response?.data;
        const message =
            errorMessageFromValue(payload?.detail) ||
            errorMessageFromValue(payload?.error) ||
            payload?.message ||
            error.message ||
            `请求失败 (${status || 500})`;
        // 在错误对象上附带 HTTP 状态码：上层（如会话校验）可据此区分
        // 401（确为失效，应登出）与网络/超时/5xx（瞬时故障，不应误判登出）
        const normalizedError = new Error(message) as Error & { status?: number };
        normalizedError.status = status;
        return Promise.reject(normalizedError);
    },
);

type RequestOptions = {
    method?: string;
    body?: unknown;
    headers?: Record<string, string>;
    redirectOnUnauthorized?: boolean;
    retry?: number;
    retryDelay?: number;
};

export async function httpRequest<T>(path: string, options: RequestOptions = {}) {
    const {method = "GET", body, headers, redirectOnUnauthorized = true, retry, retryDelay} = options;
    const isIdempotent = method.toUpperCase() === "GET";
    const config: RequestConfig = {
        url: path,
        method,
        data: body,
        headers,
        redirectOnUnauthorized,
        // 幂等的 GET 默认重试 2 次；非幂等请求需显式开启，避免重复提交副作用
        retry: retry ?? (isIdempotent ? 2 : 0),
        retryDelay,
    };
    const response = await request.request<T>(config);
    return response.data;
}
