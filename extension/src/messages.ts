import type { ConnectionState } from "../../shared/state.js";
import type { UidCache } from "../../shared/uid-cache.js";
import type { VideoTask } from "../../shared/types.js";
import type { VideoIdentity } from "./video.js";

export type RuntimeMessage =
  | { type: "GET_UID_CACHE" }
  | { type: "GET_POPUP_STATE" }
  | { type: "SYNC_UID_CACHE" }
  | { type: "SUBMIT_CURRENT_VIDEO"; expectedBvid?: string };

export interface PopupState {
  video: VideoIdentity | null;
  connection: ConnectionState;
  cache: {
    available: boolean;
    version: number;
    count: number;
    lastSyncedAt?: string;
  };
  task?: VideoTask;
}

export type RuntimeResponse =
  | { ok: true; cache: UidCache }
  | { ok: true; popup: PopupState }
  | { ok: true; task: VideoTask }
  | { ok: true; synced: boolean; cache: UidCache; connection: ConnectionState }
  | { ok: false; error: { status?: number; message: string } };
