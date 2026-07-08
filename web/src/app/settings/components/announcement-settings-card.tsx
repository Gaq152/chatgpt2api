"use client";

import { LoaderCircle, Megaphone, Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";

import { useSettingsStore } from "../store";

export function AnnouncementSettingsCard() {
  const config = useSettingsStore((state) => state.config);
  const isLoadingConfig = useSettingsStore((state) => state.isLoadingConfig);
  const isSavingAnnouncement = useSettingsStore((state) => state.isSavingAnnouncement);
  const setAnnouncementEnabled = useSettingsStore((state) => state.setAnnouncementEnabled);
  const setAnnouncementMessage = useSettingsStore((state) => state.setAnnouncementMessage);
  const saveAnnouncement = useSettingsStore((state) => state.saveAnnouncement);
  const announcement = config?.announcement || { enabled: false, message: "" };

  if (isLoadingConfig) {
    return (
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="flex items-center justify-center p-8">
          <LoaderCircle className="size-5 animate-spin text-stone-400" />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-4 p-6">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex items-start gap-3">
            <div className="flex size-10 shrink-0 items-center justify-center rounded-2xl bg-amber-100 text-amber-700">
              <Megaphone className="size-5" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-stone-950">画图页公告</h2>
              <p className="mt-1 text-sm leading-6 text-stone-500">
                发布后显示在画图页面顶部；开启但内容为空时不会展示。
              </p>
            </div>
          </div>
          <label className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
            <Checkbox
              checked={Boolean(announcement.enabled)}
              onCheckedChange={(checked) => setAnnouncementEnabled(Boolean(checked))}
            />
            发布公告
          </label>
        </div>

        <Textarea
          value={String(announcement.message || "")}
          onChange={(event) => setAnnouncementMessage(event.target.value)}
          placeholder="例如：今晚 22:00-23:00 进行维护，期间出图可能变慢。"
          className="min-h-28 rounded-xl border-stone-200 bg-white text-sm shadow-none"
        />

        <div className="flex justify-end">
          <Button
            className="h-10 rounded-xl bg-amber-600 px-5 text-white hover:bg-amber-700"
            onClick={() => void saveAnnouncement()}
            disabled={isSavingAnnouncement}
          >
            {isSavingAnnouncement ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
            保存公告
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
