import { apiBaseUrl } from "@/lib/config";
import { FixtureViewerAdapter } from "./fixture-adapter";
import { HttpViewerAdapter } from "./http-adapter";
import type { ViewerAdapter } from "./types";

let cached: ViewerAdapter | null = null;

/**
 * The single entry point screens use to reach data.
 *
 * Selection is by configuration only: with `NEXT_PUBLIC_VIEWER_API_BASE_URL`
 * set the Viewer talks to the live query API, and without it the deterministic
 * fixture adapter runs and every screen is badged "Demo Data". Both satisfy the
 * same interface, so swapping one for the other changes no component.
 */
export function getViewerAdapter(): ViewerAdapter {
  if (cached) return cached;
  const base = apiBaseUrl();
  cached = base ? new HttpViewerAdapter(base) : new FixtureViewerAdapter();
  return cached;
}

/** Test seam: drops the memoised adapter so configuration can be re-read. */
export function resetViewerAdapter(): void {
  cached = null;
}

export { FixtureViewerAdapter } from "./fixture-adapter";
export { HttpViewerAdapter } from "./http-adapter";
export { ViewerApiError, isAbortError } from "./types";
export type { AdapterMode, ViewerAdapter, ViewerErrorKind } from "./types";
