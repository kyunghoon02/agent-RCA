import type {
  IncidentDetail,
  IncidentListQuery,
  IncidentListResult,
  IncidentWorkState,
} from "@/lib/types";

/**
 * How the currently active adapter sources its data.
 * `fixture` is surfaced to operators as a "Demo Data" badge so a demo is never
 * mistaken for a live cluster.
 */
export type AdapterMode = "live" | "fixture";

/**
 * The only data-access surface screens are allowed to use.
 *
 * There is deliberately no create/update/delete member: the Viewer is
 * read-only, and that constraint is expressed in the type system rather than
 * left to convention.
 */
export interface ViewerAdapter {
  readonly mode: AdapterMode;
  listIncidents(
    query: IncidentListQuery,
    signal?: AbortSignal,
  ): Promise<IncidentListResult>;
  getIncidentDetail(incidentId: string, signal?: AbortSignal): Promise<IncidentDetail>;
  getIncidentWorkState(
    incidentId: string,
    signal?: AbortSignal,
  ): Promise<IncidentWorkState>;
}

export type ViewerErrorKind = "network" | "http" | "not-found" | "contract";

export class ViewerApiError extends Error {
  readonly kind: ViewerErrorKind;
  readonly statusCode?: number;

  constructor(message: string, kind: ViewerErrorKind, statusCode?: number) {
    super(message);
    this.name = "ViewerApiError";
    this.kind = kind;
    this.statusCode = statusCode;
  }
}

/** True for the AbortController signal raised when a stale request is cancelled. */
export function isAbortError(error: unknown): boolean {
  return (
    error instanceof DOMException && error.name === "AbortError"
  ) || (error instanceof Error && error.name === "AbortError");
}
