import type { SampleItem, SampleKind } from "../../shared/types.js";

export function parseSampleText(text: string, kind: SampleKind, source: "manual" | "file" = "manual"): SampleItem[] {
  const items: SampleItem[] = [];
  const seen = new Set<string>();
  for (const line of text.split(/\r?\n/)) {
    const value = line.trim();
    if (!value || value.startsWith("#")) continue;
    const key = `${kind}\u0000${value}`;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({ text: value, kind, source });
  }
  return items;
}

export function mergeSampleItems(...groups: SampleItem[][]): SampleItem[] {
  const merged: SampleItem[] = [];
  const seen = new Set<string>();
  for (const group of groups) {
    for (const item of group) {
      const value = item.text.trim();
      if (!value) continue;
      const key = `${item.kind}\u0000${value}`;
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push({ ...item, text: value });
    }
  }
  return merged;
}
