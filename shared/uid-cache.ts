import type { UidRecord, UidSyncResponse } from "./types.js";

export interface UidCache {
  version: number;
  records: Record<string, UidRecord>;
  lastSyncedAt?: string;
  requiresSnapshot?: boolean;
}

export function createEmptyUidCache(): UidCache {
  return { version: 0, records: {} };
}

export function shouldHideUid(cache: UidCache, uid: string): boolean {
  const record = cache.records[uid];
  return Boolean(record && record.hidden && record.status !== "exception");
}

export function applyUidSync(cache: UidCache, sync: UidSyncResponse, syncedAt = new Date().toISOString()): UidCache {
  if (!Number.isInteger(sync.version) || sync.version < cache.version) return cache;

  if (sync.mode === "snapshot") {
    return {
      version: sync.version,
      records: indexRecords(sync.records ?? []),
      lastSyncedAt: syncedAt,
      requiresSnapshot: false,
    };
  }

  if (sync.baseVersion !== undefined && sync.baseVersion !== cache.version) {
    return { ...cache, requiresSnapshot: true };
  }

  const records = { ...cache.records };
  for (const record of sync.upserts ?? []) records[record.uid] = record;
  for (const uid of sync.removals ?? []) delete records[uid];
  return {
    version: sync.version,
    records,
    lastSyncedAt: syncedAt,
    requiresSnapshot: false,
  };
}

export function serializeUidCache(cache: UidCache): string {
  return JSON.stringify(cache);
}

export function deserializeUidCache(value: string): UidCache {
  const parsed: unknown = JSON.parse(value);
  if (!isUidCache(parsed)) throw new Error("UID 缓存格式无效");
  return parsed;
}

function indexRecords(records: UidRecord[]): Record<string, UidRecord> {
  return Object.fromEntries(records.filter((record) => record.uid.trim()).map((record) => [record.uid, record]));
}

function isUidCache(value: unknown): value is UidCache {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<UidCache>;
  return (
    Number.isInteger(candidate.version) &&
    Boolean(candidate.records) &&
    typeof candidate.records === "object" &&
    Object.values(candidate.records as Record<string, unknown>).every(isUidRecord)
  );
}

function isUidRecord(value: unknown): value is UidRecord {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<UidRecord>;
  return (
    typeof candidate.uid === "string" &&
    typeof candidate.nicknameSnapshot === "string" &&
    typeof candidate.status === "string" &&
    typeof candidate.hidden === "boolean" &&
    typeof candidate.updatedAt === "string"
  );
}
