import type { EvidenceItem } from "./types";

export type EvidenceCompleteness = "complete" | "partial" | "insufficient";

/**
 * Classifies how much an Evidence item actually establishes.
 *
 * Collectors mark a degraded result two ways: a `status` marker inside `facts`
 * and a `quality.completeness` score. Either is enough to stop the Viewer from
 * presenting the item as a full observation.
 */
export function evidenceCompleteness(item: EvidenceItem): EvidenceCompleteness {
  const marker = item.facts?.["status"];
  if (marker === "INSUFFICIENT_DATA") return "insufficient";
  if (item.quality.completeness <= 0) return "insufficient";
  if (marker === "PARTIAL" || item.quality.completeness < 1) return "partial";
  return "complete";
}

export interface InsufficiencyDetail {
  reason: string | null;
  detail: string | null;
  missingSeries: string[];
  observed: { label: string; value: string }[];
}

/** Pulls the "why is this incomplete" fields out of `facts` for direct display. */
export function insufficiencyDetail(item: EvidenceItem): InsufficiencyDetail {
  const facts = item.facts ?? {};
  const reason = typeof facts["reason"] === "string" ? facts["reason"] : null;
  const detail = typeof facts["detail"] === "string" ? facts["detail"] : null;
  const rawSeries = facts["missing_series"];
  const missingSeries = Array.isArray(rawSeries)
    ? rawSeries.filter((entry): entry is string => typeof entry === "string")
    : [];

  const observed: { label: string; value: string }[] = [];
  const counters: [string, string][] = [
    ["expected_sample_count", "Expected samples"],
    ["returned_sample_count", "Returned samples"],
    ["shards_queried", "Shards queried"],
    ["shards_returned", "Shards returned"],
    ["sampled_traces", "Sampled traces"],
    ["required_traces", "Required traces"],
  ];
  for (const [key, label] of counters) {
    const value = facts[key];
    if (typeof value === "number") observed.push({ label, value: String(value) });
  }

  return { reason, detail, missingSeries, observed };
}

/** Distinct sources present, for the source filter. */
export function distinctValues<K extends keyof EvidenceItem>(
  items: EvidenceItem[],
  key: K,
): string[] {
  return [...new Set(items.map((item) => String(item[key])))].sort();
}
