import type { TimelineEvent, TimelineStage } from "./types";

/**
 * Milestones are the events an operator scans for: what happened to the
 * Incident, not what each collector observed.
 */
const MILESTONE_EVENT_TYPES = new Set([
  "INCIDENT_CREATED",
  "STATUS_TRANSITIONED",
  "COLLECTION_COMPLETED",
  "COLLECTION_FAILED",
  "CONTEXT_FROZEN",
  "ALERT_RESOLVED",
  "REPORT_GENERATED",
  "AGENT_RUN_COMPLETED",
]);

/** Work-queue lifecycle events, whatever prefix the backend gives them. */
function isWorkEvent(eventType: string): boolean {
  return (
    eventType.includes("WORK_CLAIMED") ||
    eventType.includes("WORK_COMPLETED") ||
    eventType.includes("WORK_FAILED") ||
    eventType.endsWith("_CLAIMED") ||
    eventType.endsWith("_COMPLETED") ||
    eventType.endsWith("_FAILED")
  );
}

export function isMilestone(event: TimelineEvent): boolean {
  if (event.event_type === "EVIDENCE_OBSERVED") return false;
  return MILESTONE_EVENT_TYPES.has(event.event_type) || isWorkEvent(event.event_type);
}

/** Marks a batch whose payload carries no pass identity at all. */
export const UNKNOWN_PASS = "unknown";

/**
 * Which collection pass produced an observation.
 *
 * Position in the Timeline cannot answer this. The API builds
 * EVIDENCE_OBSERVED from Evidence `observed_at` — the instant the signal
 * describes — and then sorts everything by `occurred_at`, so a retry that reads
 * a historical metric sorts *before* the previous attempt's completion event.
 * Segmenting on milestone position would file it under the wrong attempt.
 *
 * `collected_at` is the stored time the Provider run finished, and the
 * collector stamps one value per run, so per Provider it is constant within a
 * pass and differs across passes. A persisted attempt number exists on work
 * audit events but is not recorded per Evidence, so this is the safe stable
 * identifier available. `claim_token` and lease values are never read.
 *
 * Returns UNKNOWN_PASS for older payloads that predate the field.
 */
export function collectionPassOf(event: TimelineEvent): string {
  for (const key of ["collection_attempt", "collection_work_id", "collected_at"]) {
    const value = event.details?.[key];
    if (typeof value === "string" || typeof value === "number") return String(value);
  }
  return UNKNOWN_PASS;
}

export interface TimelineGroup {
  kind: "group";
  id: string;
  stage: TimelineStage;
  eventType: string;
  /** Provider the batch came from, when the events record one. */
  provider: string | null;
  /** Stable collection-pass identity, or UNKNOWN_PASS for older payloads. */
  pass: string;
  /**
   * False when the payload carried no pass identity. The UI must not claim
   * retries were separated in that case.
   */
  passKnown: boolean;
  startedAt: string;
  endedAt: string;
  events: TimelineEvent[];
  evidenceIds: string[];
  label: string;
}

export interface TimelineSingle {
  kind: "event";
  id: string;
  occurredAt: string;
  event: TimelineEvent;
}

export type TimelineEntry = TimelineGroup | TimelineSingle;

export function entryTimestamp(entry: TimelineEntry): string {
  return entry.kind === "group" ? entry.startedAt : entry.occurredAt;
}

function providerOf(event: TimelineEvent): string | null {
  const source = event.details?.["source"];
  if (typeof source === "string") return source;
  const provider = event.details?.["provider"];
  return typeof provider === "string" ? provider : null;
}

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/**
 * Deliberately "observed", not "collected".
 *
 * The event timestamp is the Evidence `observed_at` — the instant the signal
 * describes — which routinely precedes the Incident itself. Calling it
 * collection would assert an execution time the event does not carry.
 */
function describeBatch(provider: string | null, count: number): string {
  const item = count === 1 ? "Evidence item" : "Evidence items";
  const who = provider ? titleCase(provider) : "Providers";
  return `${who} observed ${count} ${item}`;
}

/**
 * Folds per-Evidence rows into one row per Provider, keeping milestones intact.
 *
 * A live Incident's timeline is dominated by one EVIDENCE_OBSERVED row per
 * Evidence item — 49 of 60 events on a real Incident — which buries the
 * lifecycle transitions that actually explain where the pipeline is. Providers
 * collect concurrently, so their rows interleave and consecutive-run batching
 * barely helps; grouping by Provider across the collection phase is what
 * restores the signal.
 *
 * Nothing is discarded: every group carries its member events, and each shows
 * the real time span it covers so two separate collection passes stay visible.
 */
export function groupTimeline(timeline: readonly TimelineEvent[]): TimelineEntry[] {
  const groups = new Map<string, TimelineGroup>();
  const entries: TimelineEntry[] = [];

  timeline.forEach((event, index) => {
    if (isMilestone(event)) {
      entries.push({
        kind: "event",
        id: `${event.occurred_at}-${event.event_type}-${index}`,
        occurredAt: event.occurred_at,
        event,
      });
      return;
    }

    const provider = providerOf(event);
    // Pass identity comes from the event payload, never from array position.
    const pass = collectionPassOf(event);
    const key = `${event.stage}|${event.event_type}|${provider ?? ""}|${pass}`;
    const existing = groups.get(key);
    if (existing) {
      existing.events.push(event);
      existing.evidenceIds.push(...event.evidence_ids);
      if (event.occurred_at < existing.startedAt) existing.startedAt = event.occurred_at;
      if (event.occurred_at > existing.endedAt) existing.endedAt = event.occurred_at;
      return;
    }

    const group: TimelineGroup = {
      kind: "group",
      // Pass-qualified and derived from stored values, so ids stay stable
      // across refetches regardless of how the API ordered the events.
      id: key,
      stage: event.stage,
      eventType: event.event_type,
      provider,
      pass,
      passKnown: pass !== UNKNOWN_PASS,
      startedAt: event.occurred_at,
      endedAt: event.occurred_at,
      events: [event],
      evidenceIds: [...event.evidence_ids],
      label: "",
    };
    groups.set(key, group);
    entries.push(group);
  });

  for (const group of groups.values()) {
    group.label =
      group.eventType === "EVIDENCE_OBSERVED"
        ? describeBatch(group.provider, group.events.length)
        : `${group.events.length} × ${group.eventType}`;
    group.events.sort((left, right) => left.occurred_at.localeCompare(right.occurred_at));
  }

  return entries.sort((left, right) => {
    const at = entryTimestamp(left).localeCompare(entryTimestamp(right));
    if (at !== 0) return at;
    // Groups after milestones at the same instant, then stable by id.
    if (left.kind !== right.kind) return left.kind === "event" ? -1 : 1;
    return left.id.localeCompare(right.id);
  });
}

export function countGroupedEvents(entries: readonly TimelineEntry[]): number {
  return entries.reduce(
    (total, entry) => total + (entry.kind === "group" ? entry.events.length : 1),
    0,
  );
}


/**
 * The window in which a batch's Evidence was actually collected.
 *
 * Group timestamps come from `observed_at`, which is when the signal happened,
 * not when a Provider ran. The real execution time is already stored on each
 * Evidence item as `provenance.collected_at`, so it can be reported without
 * inventing a value or extending the API contract. Returns null when none of
 * the batch's Evidence is present in the payload.
 */
export function collectedWindow(
  evidenceIds: readonly string[],
  evidenceById: ReadonlyMap<string, { provenance: { collected_at: string } }>,
): { start: string; end: string } | null {
  let start: string | null = null;
  let end: string | null = null;
  for (const id of evidenceIds) {
    const collectedAt = evidenceById.get(id)?.provenance.collected_at;
    if (!collectedAt) continue;
    if (start === null || collectedAt < start) start = collectedAt;
    if (end === null || collectedAt > end) end = collectedAt;
  }
  return start !== null && end !== null ? { start, end } : null;
}
