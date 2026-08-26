import { DEFAULT_PAGE_SIZE } from "@/lib/config";
import { entityNamespace } from "@/lib/lifecycle";
import type {
  IncidentDetail,
  IncidentListQuery,
  IncidentListResult,
  IncidentSummary,
  IncidentWorkState,
} from "@/lib/types";
import { buildDetail, FIXTURE_RECORDS, type FixtureRecord } from "./fixtures";
import { ViewerApiError, type ViewerAdapter } from "./types";

const PROBLEM_COLLECTOR_STATUSES = new Set(["PARTIAL", "FAILED", "TIMED_OUT"]);

function summarise(record: FixtureRecord): IncidentSummary {
  const incident = record.incident;
  return {
    incident_id: incident.incident_id,
    status: incident.status,
    severity: incident.severity,
    source: incident.source,
    triggered_at: incident.triggered_at,
    updated_at: incident.updated_at,
    alert_name: incident.alert.name,
    source_entity: incident.source_entity,
    collector_problem_count: incident.collector_statuses.filter((status) =>
      PROBLEM_COLLECTOR_STATUSES.has(status.status),
    ).length,
  };
}

/**
 * Stable, non-cryptographic digest of the active filters.
 *
 * The server binds its cursor to a SHA-256 filter hash so a cursor cannot be
 * replayed against different filters. The fixture adapter reproduces that
 * behaviour — the algorithm differs, the guarantee does not.
 */
function filterHash(query: IncidentListQuery): string {
  const canonical = JSON.stringify({
    statuses: [...query.statuses].sort(),
    severities: [...query.severities].sort(),
    namespace: query.namespace,
    search: query.search,
  });
  let value = 0x811c9dc5;
  for (let index = 0; index < canonical.length; index += 1) {
    value ^= canonical.charCodeAt(index);
    value = Math.imul(value, 0x01000193) >>> 0;
  }
  return value.toString(16).padStart(8, "0");
}

interface CursorPayload {
  updated_at: string;
  incident_id: string;
  filter_hash: string;
}

function encodeCursor(payload: CursorPayload): string {
  return btoa(JSON.stringify(payload)).replace(/=+$/, "");
}

function decodeCursor(cursor: string, expectedHash: string): CursorPayload {
  let payload: CursorPayload;
  try {
    payload = JSON.parse(atob(cursor));
  } catch {
    throw new ViewerApiError("Viewer cursor is malformed", "contract");
  }
  if (payload?.filter_hash !== expectedHash) {
    throw new ViewerApiError("Viewer cursor does not match current filters", "contract");
  }
  return payload;
}

function matches(record: FixtureRecord, query: IncidentListQuery): boolean {
  const incident = record.incident;
  if (query.statuses.length && !query.statuses.includes(incident.status)) return false;
  if (query.severities.length && !query.severities.includes(incident.severity)) return false;
  if (query.namespace) {
    const namespace =
      entityNamespace(incident.source_entity) ?? incident.alert.labels["namespace"] ?? null;
    if (namespace !== query.namespace) return false;
  }
  if (query.search) {
    const needle = query.search.toLowerCase();
    const haystack = [
      incident.alert.name,
      incident.incident_id,
      incident.source_entity.name,
      incident.alert.labels["service"] ?? "",
    ]
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(needle)) return false;
  }
  return true;
}

/**
 * Deterministic adapter used whenever no Viewer API is configured.
 *
 * It reads a frozen dataset, applies exactly the filters the query contract
 * allows, and paginates with a filter-bound cursor. It never mutates the
 * dataset and never invents rows.
 */
export class FixtureViewerAdapter implements ViewerAdapter {
  readonly mode = "fixture" as const;

  private readonly records: FixtureRecord[];

  constructor(records: FixtureRecord[] = FIXTURE_RECORDS) {
    this.records = records;
  }

  async listIncidents(query: IncidentListQuery): Promise<IncidentListResult> {
    const limit = Math.min(Math.max(query.limit || DEFAULT_PAGE_SIZE, 1), 100);
    const hash = filterHash(query);
    const filtered = this.records.filter((record) => matches(record, query));

    let start = 0;
    if (query.cursor) {
      const payload = decodeCursor(query.cursor, hash);
      start = filtered.findIndex(
        (record) =>
          record.incident.updated_at < payload.updated_at ||
          (record.incident.updated_at === payload.updated_at &&
            record.incident.incident_id < payload.incident_id),
      );
      if (start < 0) start = filtered.length;
    }

    const page = filtered.slice(start, start + limit);
    const hasMore = filtered.length > start + limit;
    const last = page.at(-1);
    return {
      schema_version: "1.0.0",
      items: page.map(summarise),
      next_cursor:
        hasMore && last
          ? encodeCursor({
              updated_at: last.incident.updated_at,
              incident_id: last.incident.incident_id,
              filter_hash: hash,
            })
          : null,
    };
  }

  async getIncidentDetail(incidentId: string): Promise<IncidentDetail> {
    const record = this.records.find((item) => item.incident.incident_id === incidentId);
    if (!record) {
      throw new ViewerApiError(`Incident ${incidentId} was not found`, "not-found", 404);
    }
    return buildDetail(record);
  }

  async getIncidentWorkState(incidentId: string): Promise<IncidentWorkState> {
    const record = this.records.find((item) => item.incident.incident_id === incidentId);
    if (!record) {
      throw new ViewerApiError(`Incident ${incidentId} was not found`, "not-found", 404);
    }
    return record.work;
  }
}
