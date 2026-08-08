export interface VideoIdentity {
  bvid: string;
  url: string;
  title: string;
}

const BV_VIDEO_PATH = /^\/video\/(BV[0-9A-Za-z]{10})(?:\/|$)/i;

export function isSupportedVideoUrl(value: string): boolean {
  return getVideoIdentity(value, "") !== null;
}

export function getVideoIdentity(value: string, title = ""): VideoIdentity | null {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || !["www.bilibili.com", "bilibili.com"].includes(url.hostname.toLowerCase())) {
    return null;
  }
  const match = url.pathname.match(BV_VIDEO_PATH);
  if (!match) return null;
  const bvid = match[1];
  return {
    bvid,
    url: `https://www.bilibili.com/video/${bvid}`,
    title: title.trim(),
  };
}
