/**
 * Runtime configuration for the Viewer.
 *
 * Every value comes from a `NEXT_PUBLIC_*` variable. Nothing here is hardcoded
 * to a cluster, tenant, or observability host.
 */

/** Only these schemes may ever be turned into a rendered anchor. */
const ALLOWED_LINK_PROTOCOLS = new Set(["https:", "http:"]);

/**
 * Returns the URL only when it parses and uses an allowlisted scheme.
 * Anything else — `javascript:`, `data:`, a bare path, a typo — yields null so
 * the caller renders no link at all rather than an unsafe one.
 */
export function allowlistExternalUrl(raw: string | undefined | null): string | null {
  if (!raw) return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return null;
  }
  if (!ALLOWED_LINK_PROTOCOLS.has(parsed.protocol)) return null;
  return parsed.toString();
}

export interface DeepLink {
  label: string;
  href: string;
}

/**
 * Deep links are opt-in. A link appears only when its variable is set AND the
 * value passes the allowlist.
 */
export function deepLinks(): DeepLink[] {
  const candidates: { label: string; raw: string | undefined }[] = [
    { label: "Grafana", raw: process.env.NEXT_PUBLIC_GRAFANA_URL },
    { label: "Loki", raw: process.env.NEXT_PUBLIC_LOKI_URL },
    { label: "Tempo", raw: process.env.NEXT_PUBLIC_TEMPO_URL },
  ];
  const links: DeepLink[] = [];
  for (const candidate of candidates) {
    const href = allowlistExternalUrl(candidate.raw);
    if (href) links.push({ label: candidate.label, href });
  }
  return links;
}

/** Base URL of the read-only Viewer query API, or null when unconfigured. */
export function apiBaseUrl(): string | null {
  const raw = process.env.NEXT_PUBLIC_VIEWER_API_BASE_URL;
  if (!raw || !raw.trim()) return null;
  const value = raw.trim().replace(/\/+$/, "");
  return value === "/api/viewer" ? value : null;
}

export function environmentLabel(): string {
  const raw = process.env.NEXT_PUBLIC_VIEWER_ENVIRONMENT;
  return raw && raw.trim() ? raw.trim() : "local";
}

/** Default polling interval, in milliseconds. */
export const DEFAULT_POLL_INTERVAL_MS = 2000;

export const POLL_INTERVAL_OPTIONS = [
  { label: "2s", value: 2000 },
  { label: "5s", value: 5000 },
  { label: "15s", value: 15000 },
  { label: "60s", value: 60000 },
] as const;

export const DEFAULT_PAGE_SIZE = 25;
