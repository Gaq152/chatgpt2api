"use client";

import localforage from "localforage";

import {
  clearServerImageConversations,
  deleteServerImageConversation,
  fetchServerImageConversations,
  migrateServerImageConversations,
  renameServerImageConversation,
  saveServerImageConversation,
  uploadReferenceImage,
  type ImageModel,
  type ServerImageConversation,
  type ServerImageTurn,
  type ServerReferenceImage,
  type ServerStoredImage,
} from "@/lib/api";

export type ImageConversationMode = "generate" | "edit";

export type StoredReferenceImage = {
  name: string;
  type: string;
  dataUrl: string;
  url?: string;
};

export type StoredImage = {
  id: string;
  taskId?: string;
  status?: "loading" | "success" | "error";
  b64_json?: string;
  url?: string;
  revised_prompt?: string;
  error?: string;
};

export type ImageTurnStatus = "queued" | "generating" | "success" | "error";

export type ImageTurn = {
  id: string;
  prompt: string;
  model: ImageModel;
  mode: ImageConversationMode;
  referenceImages: StoredReferenceImage[];
  count: number;
  size: string;
  images: StoredImage[];
  createdAt: string;
  status: ImageTurnStatus;
  error?: string;
  promptDeleted?: boolean;
  resultsDeleted?: boolean;
};

export type ImageConversation = {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  turns: ImageTurn[];
};

export type ImageConversationStats = {
  queued: number;
  running: number;
};

// ---------------------------------------------------------------------------
// Local cache (IndexedDB via localforage), scoped by user
// ---------------------------------------------------------------------------

const imageConversationStorage = localforage.createInstance({
  name: "chatgpt2api",
  storeName: "image_conversations",
});

let currentSubjectId: string | null = null;
let imageConversationWriteQueue: Promise<void> = Promise.resolve();

const MIGRATION_FLAG_PREFIX = "chatgpt2api:conversations_migrated:";

function storageKey(subjectId: string | null) {
  return subjectId ? `items:${subjectId}` : "items";
}

function queueWrite<T>(operation: () => Promise<T>): Promise<T> {
  const result = imageConversationWriteQueue.then(operation);
  imageConversationWriteQueue = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

// ---------------------------------------------------------------------------
// Normalization helpers (local format)
// ---------------------------------------------------------------------------

function normalizeStoredImage(image: StoredImage): StoredImage {
  const normalized = {
    ...image,
    taskId: typeof image.taskId === "string" && image.taskId ? image.taskId : undefined,
    url: typeof image.url === "string" && image.url ? image.url : undefined,
    revised_prompt: typeof image.revised_prompt === "string" ? image.revised_prompt : undefined,
  };
  if (image.status === "loading" || image.status === "error" || image.status === "success") {
    return normalized;
  }
  return {
    ...normalized,
    status: image.b64_json || image.url ? "success" : "loading",
  };
}

function normalizeReferenceImage(image: StoredReferenceImage): StoredReferenceImage {
  return {
    name: image.name || "reference.png",
    type: image.type || "image/png",
    dataUrl: image.dataUrl || "",
    url: typeof image.url === "string" && image.url ? image.url : undefined,
  };
}

function dataUrlMimeType(dataUrl: string) {
  const match = dataUrl.match(/^data:(.*?);base64,/);
  return match?.[1] || "image/png";
}

function getLegacyReferenceImages(source: Record<string, unknown>): StoredReferenceImage[] {
  if (Array.isArray(source.referenceImages)) {
    return source.referenceImages
      .filter((image): image is StoredReferenceImage => {
        if (!image || typeof image !== "object") {
          return false;
        }
        const candidate = image as StoredReferenceImage;
        return (typeof candidate.dataUrl === "string" && candidate.dataUrl.length > 0) ||
          (typeof candidate.url === "string" && candidate.url.length > 0);
      })
      .map(normalizeReferenceImage);
  }

  if (source.sourceImage && typeof source.sourceImage === "object") {
    const image = source.sourceImage as { dataUrl?: unknown; fileName?: unknown };
    if (typeof image.dataUrl === "string" && image.dataUrl) {
      return [
        {
          name: typeof image.fileName === "string" && image.fileName ? image.fileName : "reference.png",
          type: dataUrlMimeType(image.dataUrl),
          dataUrl: image.dataUrl,
        },
      ];
    }
  }

  return [];
}

function normalizeTurn(turn: ImageTurn & Record<string, unknown>): ImageTurn {
  const normalizedImages = Array.isArray(turn.images) ? turn.images.map(normalizeStoredImage) : [];
  const derivedStatus: ImageTurnStatus =
    normalizedImages.some((image) => image.status === "loading")
      ? "generating"
      : normalizedImages.some((image) => image.status === "error")
        ? "error"
        : "success";

  return {
    id: String(turn.id || `${Date.now()}`),
    prompt: String(turn.prompt || ""),
    model: (turn.model as ImageModel) || "gpt-image-2",
    mode: turn.mode === "edit" ? "edit" : "generate",
    referenceImages: getLegacyReferenceImages(turn),
    count: Math.max(1, Number(turn.count || normalizedImages.length || 1)),
    size: typeof turn.size === "string" ? turn.size : "",
    images: normalizedImages,
    createdAt: String(turn.createdAt || new Date().toISOString()),
    status:
      turn.status === "queued" ||
      turn.status === "generating" ||
      turn.status === "success" ||
      turn.status === "error"
        ? turn.status
        : derivedStatus,
    error: typeof turn.error === "string" ? turn.error : undefined,
    promptDeleted: turn.promptDeleted === true,
    resultsDeleted: turn.resultsDeleted === true,
  };
}

function normalizeConversation(conversation: ImageConversation & Record<string, unknown>): ImageConversation {
  const turns = Array.isArray(conversation.turns)
    ? conversation.turns.map((turn) => normalizeTurn(turn as ImageTurn & Record<string, unknown>))
    : [
        normalizeTurn({
          id: String(conversation.id || `${Date.now()}`),
          prompt: String(conversation.prompt || ""),
          model: (conversation.model as ImageModel) || "gpt-image-2",
          mode: conversation.mode === "edit" ? "edit" : "generate",
          referenceImages: getLegacyReferenceImages(conversation),
          count: Number(conversation.count || 1),
          size: typeof conversation.size === "string" ? conversation.size : "",
          images: Array.isArray(conversation.images) ? (conversation.images as StoredImage[]) : [],
          createdAt: String(conversation.createdAt || new Date().toISOString()),
          status:
            conversation.status === "generating" || conversation.status === "success" || conversation.status === "error"
              ? conversation.status
              : "success",
          error: typeof conversation.error === "string" ? conversation.error : undefined,
        }),
      ];
  const lastTurn = turns.length > 0 ? turns[turns.length - 1] : null;

  return {
    id: String(conversation.id || `${Date.now()}`),
    title: String(conversation.title || ""),
    createdAt: String(conversation.createdAt || lastTurn?.createdAt || new Date().toISOString()),
    updatedAt: String(conversation.updatedAt || lastTurn?.createdAt || new Date().toISOString()),
    turns,
  };
}

function sortConversations(conversations: ImageConversation[]): ImageConversation[] {
  return [...conversations].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

function getTimestamp(value: string) {
  const time = new Date(value).getTime();
  return Number.isFinite(time) ? time : 0;
}

function pickLatest(current: ImageConversation, next: ImageConversation) {
  return getTimestamp(next.updatedAt) >= getTimestamp(current.updatedAt) ? next : current;
}

// ---------------------------------------------------------------------------
// Server ↔ local format conversion
// ---------------------------------------------------------------------------

function serverTurnToLocal(turn: ServerImageTurn): ImageTurn {
  return {
    id: turn.id,
    prompt: turn.prompt,
    model: (turn.model as ImageModel) || "gpt-image-2",
    mode: turn.mode === "edit" ? "edit" : "generate",
    referenceImages: (turn.reference_images || []).map((ref) => ({
      name: ref.name,
      type: ref.type,
      dataUrl: "",
      url: ref.url,
    })),
    count: turn.count,
    size: turn.size || "",
    images: (turn.images || []).map((img) => ({
      id: img.id,
      taskId: img.task_id,
      status: img.status,
      url: img.url,
      revised_prompt: img.revised_prompt,
      error: img.error,
    })),
    createdAt: turn.created_at,
    status: turn.status,
    error: turn.error,
    promptDeleted: turn.prompt_deleted,
    resultsDeleted: turn.results_deleted,
  };
}

function serverToLocal(conv: ServerImageConversation): ImageConversation {
  return {
    id: conv.id,
    title: conv.title,
    createdAt: conv.created_at,
    updatedAt: conv.updated_at,
    turns: (conv.turns || []).map(serverTurnToLocal),
  };
}

function localRefToServer(ref: StoredReferenceImage): ServerReferenceImage | null {
  const url = ref.url || "";
  if (!url) return null;
  return { name: ref.name, type: ref.type, url };
}

function localImageToServer(img: StoredImage): ServerStoredImage {
  return {
    id: img.id,
    task_id: img.taskId,
    status: img.status,
    url: img.url,
    revised_prompt: img.revised_prompt,
    error: img.error,
  };
}

function localTurnToServer(turn: ImageTurn): ServerImageTurn {
  return {
    id: turn.id,
    prompt: turn.prompt,
    model: turn.model,
    mode: turn.mode,
    reference_images: turn.referenceImages
      .map(localRefToServer)
      .filter((ref): ref is ServerReferenceImage => ref !== null),
    count: turn.count,
    size: turn.size,
    images: turn.images.map(localImageToServer),
    created_at: turn.createdAt,
    status: turn.status,
    error: turn.error,
    prompt_deleted: turn.promptDeleted,
    results_deleted: turn.resultsDeleted,
  };
}

function localToServer(conv: ImageConversation): ServerImageConversation {
  return {
    id: conv.id,
    title: conv.title,
    created_at: conv.createdAt,
    updated_at: conv.updatedAt,
    turns: conv.turns.map(localTurnToServer),
  };
}

async function uploadPendingReferences(conv: ImageConversation): Promise<ImageConversation> {
  let changed = false;
  const turns = await Promise.all(
    conv.turns.map(async (turn) => {
      const refs = await Promise.all(
        turn.referenceImages.map(async (ref) => {
          if (ref.url) return ref;
          if (!ref.dataUrl) return ref;
          try {
            const [header, content] = ref.dataUrl.split(",", 2);
            const matchedMime = header.match(/data:(.*?);base64/)?.[1];
            const binary = atob(content || "");
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i += 1) {
              bytes[i] = binary.charCodeAt(i);
            }
            const file = new File([bytes], ref.name, { type: matchedMime || ref.type || "image/png" });
            const result = await uploadReferenceImage(file);
            changed = true;
            return { ...ref, url: result.url };
          } catch {
            return ref;
          }
        }),
      );
      return { ...turn, referenceImages: refs };
    }),
  );
  return changed ? { ...conv, turns } : conv;
}

// ---------------------------------------------------------------------------
// Merge helper: server items + local items → merged
// ---------------------------------------------------------------------------

function mergeConversations(local: ImageConversation[], server: ImageConversation[]): ImageConversation[] {
  const map = new Map<string, ImageConversation>();
  for (const item of local) {
    map.set(item.id, item);
  }
  for (const item of server) {
    const existing = map.get(item.id);
    map.set(item.id, existing ? pickLatest(existing, item) : item);
  }
  return sortConversations([...map.values()]);
}

// ---------------------------------------------------------------------------
// Local cache read/write
// ---------------------------------------------------------------------------

async function readLocalCache(subjectId: string | null): Promise<ImageConversation[]> {
  const key = storageKey(subjectId);
  const items =
    (await imageConversationStorage.getItem<Array<ImageConversation & Record<string, unknown>>>(key)) || [];
  return items.map(normalizeConversation);
}

async function writeLocalCache(subjectId: string | null, conversations: ImageConversation[]): Promise<void> {
  await imageConversationStorage.setItem(storageKey(subjectId), sortConversations(conversations));
}

// ---------------------------------------------------------------------------
// Migration: old unscoped "items" → server
// ---------------------------------------------------------------------------

async function maybeMigrate(subjectId: string): Promise<void> {
  const flag = MIGRATION_FLAG_PREFIX + subjectId;
  if (typeof window !== "undefined" && window.localStorage.getItem(flag)) return;

  try {
    const oldItems =
      (await imageConversationStorage.getItem<Array<ImageConversation & Record<string, unknown>>>("items")) || [];
    if (!oldItems || oldItems.length === 0) {
      if (typeof window !== "undefined") window.localStorage.setItem(flag, "1");
      return;
    }

    const normalized = oldItems.map(normalizeConversation);
    const uploaded = await Promise.all(
      normalized.map((conv: ImageConversation) => uploadPendingReferences(conv)),
    );
    const serverItems: ServerImageConversation[] = uploaded.map((conv: ImageConversation) => {
      const stripped: ImageConversation = {
        ...conv,
        turns: conv.turns.map((turn: ImageTurn) => ({
          ...turn,
          referenceImages: turn.referenceImages
            .filter((ref: StoredReferenceImage) => ref.url)
            .map((ref: StoredReferenceImage) => ({ ...ref, dataUrl: "" })),
          images: turn.images.map((img: StoredImage) => ({
            ...img,
            b64_json: undefined,
          })),
        })),
      };
      return localToServer(stripped);
    });

    await migrateServerImageConversations(serverItems);
    await imageConversationStorage.removeItem("items");
  } catch {
    return;
  }

  if (typeof window !== "undefined") window.localStorage.setItem(flag, "1");
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function initImageConversations(subjectId: string): Promise<ImageConversation[]> {
  currentSubjectId = subjectId;

  await maybeMigrate(subjectId);

  const localItems = await readLocalCache(subjectId);

  try {
    const serverData = await fetchServerImageConversations();
    const serverItems = serverData.items.map(serverToLocal);
    const merged = mergeConversations(localItems, serverItems);
    await writeLocalCache(subjectId, merged);
    return merged;
  } catch {
    return sortConversations(localItems);
  }
}

export async function listImageConversations(): Promise<ImageConversation[]> {
  return sortConversations(await readLocalCache(currentSubjectId));
}

export async function saveImageConversations(conversations: ImageConversation[]): Promise<void> {
  await queueWrite(async () => {
    const items = await readLocalCache(currentSubjectId);
    const map = new Map(items.map((item) => [item.id, item]));
    for (const conversation of conversations.map(normalizeConversation)) {
      const current = map.get(conversation.id);
      map.set(conversation.id, current ? pickLatest(current, conversation) : conversation);
    }
    const merged = sortConversations([...map.values()]);
    await writeLocalCache(currentSubjectId, merged);
  });
}

export async function saveImageConversation(conversation: ImageConversation): Promise<void> {
  await queueWrite(async () => {
    const items = await readLocalCache(currentSubjectId);
    const nextConversation = normalizeConversation(conversation);
    const current = items.find((item) => item.id === nextConversation.id);
    const persisted = current ? pickLatest(current, nextConversation) : nextConversation;
    const nextItems = sortConversations([
      persisted,
      ...items.filter((item) => item.id !== persisted.id),
    ]);
    await writeLocalCache(currentSubjectId, nextItems);

    try {
      const uploaded = await uploadPendingReferences(persisted);
      await saveServerImageConversation(localToServer(uploaded));
    } catch {
      // server sync failed — local cache is still up to date
    }
  });
}

export async function renameImageConversation(id: string, title: string): Promise<void> {
  await queueWrite(async () => {
    const items = await readLocalCache(currentSubjectId);
    const target = items.find((item) => item.id === id);
    if (!target) return;
    const updated = { ...target, title, updatedAt: new Date().toISOString() };
    const nextItems = sortConversations([
      updated,
      ...items.filter((item) => item.id !== id),
    ]);
    await writeLocalCache(currentSubjectId, nextItems);

    try {
      await renameServerImageConversation(id, title);
    } catch {
      // server sync failed
    }
  });
}

export async function deleteImageConversation(id: string): Promise<void> {
  await queueWrite(async () => {
    const items = await readLocalCache(currentSubjectId);
    await writeLocalCache(
      currentSubjectId,
      items.filter((item) => item.id !== id),
    );

    try {
      await deleteServerImageConversation(id);
    } catch {
      // server sync failed
    }
  });
}

export async function clearImageConversations(): Promise<void> {
  await queueWrite(async () => {
    await imageConversationStorage.removeItem(storageKey(currentSubjectId));

    try {
      await clearServerImageConversations();
    } catch {
      // server sync failed
    }
  });
}

export function getImageConversationStats(conversation: ImageConversation | null): ImageConversationStats {
  if (!conversation) {
    return { queued: 0, running: 0 };
  }

  return conversation.turns.reduce(
    (acc, turn) => {
      if (turn.resultsDeleted) {
        return acc;
      }
      if (turn.status === "queued") {
        acc.queued += 1;
      } else if (turn.status === "generating") {
        acc.running += 1;
      }
      return acc;
    },
    { queued: 0, running: 0 },
  );
}
