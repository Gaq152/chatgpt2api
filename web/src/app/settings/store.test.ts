import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SettingsConfig } from "@/lib/api";

const apiMocks = vi.hoisted(() => ({
  updateSettingsConfig: vi.fn(),
}));

const toastMocks = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    updateSettingsConfig: apiMocks.updateSettingsConfig,
  };
});

vi.mock("sonner", () => ({
  toast: toastMocks,
}));

import { useSettingsStore } from "./store";

function createConfig(announcement = { enabled: true, message: "未保存的公告" }): SettingsConfig {
  return {
    proxy: "",
    base_url: "",
    global_system_prompt: "",
    sensitive_words: [],
    ai_review: {
      enabled: false,
      base_url: "",
      api_key: "",
      model: "",
      prompt: "",
    },
    refresh_account_interval_minute: 5,
    image_retention_days: 30,
    image_poll_timeout_secs: 120,
    image_account_concurrency: 3,
    auto_remove_invalid_accounts: false,
    auto_remove_rate_limited_accounts: false,
    auto_relogin: true,
    log_levels: [],
    image_storage: {
      enabled: false,
      mode: "local",
      webdav_url: "",
      webdav_username: "",
      webdav_password: "",
      webdav_root_path: "chatgpt2api/images",
      public_base_url: "",
    },
    backup: {
      enabled: false,
      provider: "cloudflare_r2",
      bucket: "",
      account_id: "",
      access_key_id: "",
      secret_access_key: "",
      prefix: "backups",
      interval_minutes: 360,
      rotation_keep: 0,
      encrypt: false,
      passphrase: "",
      include: {
        config: true,
        register: true,
        cpa: true,
        sub2api: true,
        logs: true,
        image_tasks: true,
        accounts_snapshot: true,
        auth_keys_snapshot: true,
        images: false,
      },
    },
    announcement,
  };
}

describe("settings store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useSettingsStore.setState({
      config: null,
      isSavingConfig: false,
      isSavingAnnouncement: false,
    });
  });

  it("does not save draft announcements through the general config save action", async () => {
    const savedAnnouncement = { enabled: false, message: "已保存的公告" };
    const currentConfig = createConfig();
    apiMocks.updateSettingsConfig.mockResolvedValue({
      config: createConfig(savedAnnouncement),
    });
    useSettingsStore.setState({ config: currentConfig });

    const result = await useSettingsStore.getState().saveConfig();

    expect(result).toBe(true);
    expect(apiMocks.updateSettingsConfig).toHaveBeenCalledTimes(1);
    expect(apiMocks.updateSettingsConfig.mock.calls[0][0]).not.toHaveProperty("announcement");
    expect(useSettingsStore.getState().config?.announcement).toEqual(savedAnnouncement);
  });
});
