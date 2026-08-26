import type {
  IncidentDetail,
  IncidentListQuery,
  IncidentListResult,
  IncidentWorkState,
} from "@/lib/types";
import { ViewerApiError, type ViewerAdapter } from "./types";

/**
 * Talks to the read-only Viewer query API described in contracts/viewer.md.
 *
 * Only GET is issued. There is no code path in this class that can mutate
 * anything, and the bearer token the API requires is never held here — the
 * base URL is expected to point at a same-origin BFF that attaches it
 * server-side (see src/app/api/viewer/[...path]/route.ts).
 */
export class HttpViewerAdapter implements ViewerAdapter {
  readonly mode = "live" as const;

  private readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  async listIncidents(
    query: IncidentListQuery,
    signal?: AbortSignal,
  ): Promise<IncidentListResult> {
    const params = new URLSearchParams();
    // The API rejects unknown and duplicated single-valued parameters, so only
    // the contract's parameters are ever appended.
    for (const status of query.statuses) params.append("status", status);
    for (const severity of query.severities) params.append("severity", severity);
    if (query.namespace) params.set("namespace", query.namespace);
    if (query.search) params.set("search", query.search);
    params.set("limit", String(query.limit));
    if (query.cursor) params.set("cursor", query.cursor);

    return this.get<IncidentListResult>(`/api/v1/incidents?${params.toString()}`, signal);
  }

  async getIncidentDetail(
    incidentId: string,
    signal?: AbortSignal,
  ): Promise<IncidentDetail> {
    return this.get<IncidentDetail>(
      `/api/v1/incidents/${encodeURIComponent(incidentId)}`,
      signal,
    );
  }

  async getIncidentWorkState(
    incidentId: string,
    signal?: AbortSignal,
  ): Promise<IncidentWorkState> {
    return this.get<IncidentWorkState>(
      `/api/v1/incidents/${encodeURIComponent(incidentId)}/work`,
      signal,
    );
  }

  private async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        method: "GET",
        signal,
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") throw error;
      throw new ViewerApiError(
        error instanceof Error ? error.message : "Viewer API is unreachable",
        "network",
      );
    }

    if (!response.ok) {
      const message = await readErrorMessage(response);
      throw new ViewerApiError(
        message,
        response.status === 404 ? "not-found" : "http",
        response.status,
      );
    }

    try {
      return (await response.json()) as T;
    } catch {
      throw new ViewerApiError("Viewer API returned a malformed response", "contract");
    }
  }
}

async function readErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as {
      error?: { code?: string; message?: string };
    };
    const code = body?.error?.code;
    const message = body?.error?.message;
    if (code && message) return `${code}: ${message}`;
    if (message) return message;
  } catch {
    // Fall through to the status line.
  }
  return `Viewer API responded ${response.status}`;
}
