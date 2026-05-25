"use client";

import { useEffect, useState } from "react";
import { Copy, Eye, EyeOff, LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { fetchAccountDetail, type AccountDetail } from "@/lib/api";
import { formatTime } from "@/lib/format-time";
import { cn } from "@/lib/utils";

const SOURCE_LABELS: Record<string, string> = {
  manual: "手动上传",
  register: "注册机",
  sub2api: "Sub2API",
  cpa: "CPA",
};

function formatDateTime(value?: string | null) {
  return formatTime(value);
}

function maskValue(value: string) {
  if (!value) {
    return "—";
  }
  if (value.length <= 12) {
    return "••••••••";
  }
  return `${value.slice(0, 6)}••••••••${value.slice(-4)}`;
}

type DetailRowProps = {
  label: string;
  value?: string | number | null;
  copyable?: boolean;
};

function DetailRow({ label, value, copyable }: DetailRowProps) {
  const text = value === null || value === undefined || value === "" ? "—" : String(value);
  return (
    <div className="flex items-start justify-between gap-3 py-1.5">
      <div className="w-28 shrink-0 text-xs text-stone-500">{label}</div>
      <div className="flex min-w-0 flex-1 items-start justify-between gap-2">
        <div className="min-w-0 break-all text-sm text-stone-800">{text}</div>
        {copyable && text !== "—" ? (
          <button
            type="button"
            className="shrink-0 rounded-md p-1 text-stone-400 transition hover:bg-stone-100 hover:text-stone-700"
            onClick={() => {
              void navigator.clipboard.writeText(text);
              toast.success(`${label} 已复制`);
            }}
            aria-label={`复制 ${label}`}
          >
            <Copy className="size-3.5" />
          </button>
        ) : null}
      </div>
    </div>
  );
}

type SecretRowProps = {
  label: string;
  value?: string | null;
};

function SecretRow({ label, value }: SecretRowProps) {
  const [revealed, setRevealed] = useState(false);
  const safeValue = value || "";
  return (
    <div className="flex items-start justify-between gap-3 py-1.5">
      <div className="w-28 shrink-0 text-xs text-stone-500">{label}</div>
      <div className="flex min-w-0 flex-1 items-start justify-between gap-2">
        <code className="min-w-0 break-all rounded-md bg-stone-50 px-2 py-1 font-mono text-[12px] text-stone-700">
          {safeValue ? (revealed ? safeValue : maskValue(safeValue)) : "—"}
        </code>
        {safeValue ? (
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              className="rounded-md p-1 text-stone-400 transition hover:bg-stone-100 hover:text-stone-700"
              onClick={() => setRevealed((prev) => !prev)}
              aria-label={revealed ? `隐藏 ${label}` : `显示 ${label}`}
            >
              {revealed ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
            </button>
            <button
              type="button"
              className="rounded-md p-1 text-stone-400 transition hover:bg-stone-100 hover:text-stone-700"
              onClick={() => {
                void navigator.clipboard.writeText(safeValue);
                toast.success(`${label} 已复制`);
              }}
              aria-label={`复制 ${label}`}
            >
              <Copy className="size-3.5" />
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

type AccountDetailDialogProps = {
  accessToken: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

export function AccountDetailDialog({ accessToken, open, onOpenChange }: AccountDetailDialogProps) {
  const [detail, setDetail] = useState<AccountDetail | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    if (!open || !accessToken) {
      return;
    }
    let cancelled = false;
    setIsLoading(true);
    setDetail(null);
    fetchAccountDetail(accessToken)
      .then((data) => {
        if (!cancelled) {
          setDetail(data.item);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          toast.error(error instanceof Error ? error.message : "加载详情失败");
          onOpenChange(false);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [accessToken, open, onOpenChange]);

  const sourceLabel = detail?.source ? SOURCE_LABELS[detail.source] || detail.source : "—";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto rounded-2xl p-6 sm:max-w-2xl">
        <DialogHeader className="gap-2">
          <DialogTitle className="flex items-center gap-2 text-base">
            <span>账号详情</span>
            {detail?.email ? (
              <Badge variant="secondary" className="rounded-md font-normal">
                {detail.email}
              </Badge>
            ) : null}
          </DialogTitle>
          <DialogDescription className="text-xs leading-5">
            完整凭据仅在此处显示，请妥善保管。复制按钮可直接复制原值。
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex items-center justify-center py-10">
            <LoaderCircle className="size-5 animate-spin text-stone-400" />
          </div>
        ) : detail ? (
          <div className="space-y-5">
            <section className="space-y-1">
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-stone-400">
                基础信息
              </div>
              <div className="rounded-xl border border-stone-100 bg-white px-3 py-2">
                <DetailRow label="邮箱" value={detail.email} copyable />
                <DetailRow label="订阅类型" value={detail.type} />
                <DetailRow label="状态" value={detail.status} />
                <DetailRow
                  label="额度"
                  value={detail.image_quota_unknown ? "未知" : detail.quota}
                />
                <DetailRow label="恢复时间" value={formatDateTime(detail.restore_at)} />
                <DetailRow label="创建时间" value={formatDateTime(detail.created_at)} />
                <DetailRow label="最近使用" value={formatDateTime(detail.last_used_at)} />
                <DetailRow label="成功 / 失败" value={`${detail.success} / ${detail.fail}`} />
              </div>
            </section>

            <section className="space-y-1">
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-stone-400">
                来源
              </div>
              <div className="rounded-xl border border-stone-100 bg-white px-3 py-2">
                <DetailRow label="导入方式" value={sourceLabel} />
                {detail.account_id ? <DetailRow label="账号 ID" value={detail.account_id} copyable /> : null}
                {detail.export_type ? <DetailRow label="导出类型" value={detail.export_type} /> : null}
                {detail.source === "sub2api" && detail.source_account_id ? (
                  <DetailRow label="远端账号 ID" value={detail.source_account_id} copyable />
                ) : null}
                {detail.source === "sub2api" && detail.source_server_id ? (
                  <DetailRow label="Sub2API 服务器 ID" value={detail.source_server_id} copyable />
                ) : null}
                {detail.source === "cpa" && detail.source_pool_id ? (
                  <DetailRow label="CPA 号池 ID" value={detail.source_pool_id} copyable />
                ) : null}
                {detail.source === "cpa" && detail.source_pool_file ? (
                  <DetailRow label="CPA 文件名" value={detail.source_pool_file} copyable />
                ) : null}
                {detail.user_id ? <DetailRow label="OpenAI user_id" value={detail.user_id} copyable /> : null}
                {detail.expired ? <DetailRow label="Token 过期时间" value={detail.expired} /> : null}
                {detail.last_refresh ? <DetailRow label="上次刷新" value={detail.last_refresh} /> : null}
              </div>
            </section>

            <section className="space-y-1">
              <div className="mb-1 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-rose-500">
                凭据 (敏感信息)
              </div>
              <div className={cn("space-y-1 rounded-xl border border-rose-100 bg-rose-50/30 px-3 py-2")}>
                <SecretRow label="Access Token" value={detail.access_token} />
                <SecretRow label="Refresh Token" value={detail.refresh_token} />
                <SecretRow label="ID Token" value={detail.id_token} />
                {detail.password ? <SecretRow label="密码" value={detail.password} /> : null}
              </div>
            </section>
          </div>
        ) : null}

        <div className="flex justify-end pt-2">
          <Button
            type="button"
            variant="secondary"
            className="h-9 rounded-xl bg-stone-100 px-4 text-stone-700 hover:bg-stone-200"
            onClick={() => onOpenChange(false)}
          >
            关闭
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
