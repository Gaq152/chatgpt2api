import { Megaphone } from "lucide-react";

import type { AnnouncementSettings } from "@/lib/api";

type AnnouncementBannerProps = {
  announcement: AnnouncementSettings | null;
};

export function AnnouncementBanner({ announcement }: AnnouncementBannerProps) {
  const message = String(announcement?.message || "").trim();
  if (!announcement?.enabled || !message) {
    return null;
  }

  return (
    <div
      role="region"
      aria-label="公告"
      className="mx-auto w-full max-w-[1380px] px-0 sm:px-3"
    >
      <div className="flex gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-amber-950 shadow-sm shadow-amber-100/70 dark:border-amber-400/30 dark:bg-amber-950/35 dark:text-amber-50 dark:shadow-none">
        <div className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-full bg-amber-200/70 text-amber-900 dark:bg-amber-300/20 dark:text-amber-100">
          <Megaphone className="size-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold">公告</div>
          <div className="mt-1 whitespace-pre-wrap break-words text-sm leading-6">
            {message}
          </div>
        </div>
      </div>
    </div>
  );
}
