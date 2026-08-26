/**
 * All timestamps render in UTC.
 *
 * Operators compare these against cluster logs, and a fixed zone keeps the
 * server-rendered markup identical to the client-rendered markup.
 */

const INVALID = "—";

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return INVALID;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return INVALID;
  const iso = date.toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 19)}Z`;
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return INVALID;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return INVALID;
  return `${date.toISOString().slice(11, 19)}Z`;
}

/** Signed, coarse age relative to `now`. Callers pass `now` so tests stay fixed. */
export function formatAge(value: string | null | undefined, now: number): string {
  if (!value) return INVALID;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return INVALID;
  const deltaMs = now - date.getTime();
  const future = deltaMs < 0;
  const seconds = Math.floor(Math.abs(deltaMs) / 1000);
  const rendered = formatDurationSeconds(seconds);
  return future ? `in ${rendered}` : `${rendered} ago`;
}

function formatDurationSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

export function formatMillis(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return INVALID;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** 0.82 -> "82%". Values outside 0..1 are clamped rather than invented. */
export function formatRatio(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return INVALID;
  const clamped = Math.min(1, Math.max(0, value));
  return `${Math.round(clamped * 100)}%`;
}

export function formatInteger(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return INVALID;
  return new Intl.NumberFormat("en-US").format(value);
}

/** Short prefix of a content hash, keeping the algorithm visible. */
export function shortHash(hash: string): string {
  const [algorithm, digest] = hash.split(":");
  if (!digest) return hash;
  return `${algorithm}:${digest.slice(0, 12)}…`;
}
