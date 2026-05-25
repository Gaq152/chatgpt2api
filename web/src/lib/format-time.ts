// 后端写入的所有时间字段都是 UTC ISO（带 +00:00 / Z）。
// 前端固定按 Asia/Shanghai 渲染，跨时区访问者也看到统一的服务器时间。
// 老数据如果是不带时区的本地字符串，浏览器会按本地时区解析，结果相对今天可能偏移；
// 这种情况自然过期，不再特殊处理。

const SHANGHAI_TZ = "Asia/Shanghai";

const baseOptions: Intl.DateTimeFormatOptions = {
  timeZone: SHANGHAI_TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
};

const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", baseOptions);
const dateTimeWithSecondsFormatter = new Intl.DateTimeFormat("zh-CN", {
  ...baseOptions,
  second: "2-digit",
});
const timeOnlyFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: SHANGHAI_TZ,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const dateOnlyFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: SHANGHAI_TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function parse(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export type FormatTimeOptions = {
  withSeconds?: boolean;
  fallback?: string;
};

export function formatTime(value: string | number | Date | null | undefined, options: FormatTimeOptions = {}): string {
  const { withSeconds = false, fallback = "—" } = options;
  const date = parse(value);
  if (!date) return typeof value === "string" ? value || fallback : fallback;
  return (withSeconds ? dateTimeWithSecondsFormatter : dateTimeFormatter).format(date);
}

export function formatTimeOnly(value: string | number | Date | null | undefined, fallback = "—"): string {
  const date = parse(value);
  if (!date) return typeof value === "string" ? value || fallback : fallback;
  return timeOnlyFormatter.format(date);
}

export function formatDate(value: string | number | Date | null | undefined, fallback = "—"): string {
  const date = parse(value);
  if (!date) return typeof value === "string" ? value || fallback : fallback;
  return dateOnlyFormatter.format(date);
}
