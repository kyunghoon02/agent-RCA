import type {
  Incident,
  IncidentWorkState,
  WorkQueueState,
  WorkStage,
} from "./types";

export const WORK_STAGES: WorkStage[] = ["COLLECTION", "LOCALIZATION", "ANALYSIS"];

export const WORK_STAGE_LABELS: Record<WorkStage, string> = {
  COLLECTION: "Collection Work",
  LOCALIZATION: "Localization Work",
  ANALYSIS: "Analysis Work",
};

export function workForStage(
  work: IncidentWorkState | null | undefined,
  stage: WorkStage,
): WorkQueueState | null {
  if (!work) return null;
  if (stage === "COLLECTION") return work.collection;
  if (stage === "LOCALIZATION") return work.localization;
  return work.analysis;
}

/**
 * True when the Incident has reached ANALYZING and its analysis work is still
 * sitting READY, unclaimed.
 *
 * The work-state contract exposes no "agent enabled" flag, so this is derived
 * from the queue itself: a pinned Context with no worker and no attempts means
 * nothing has drained the queue. That is a normal waiting state — an Agent
 * runtime may simply not be deployed — and it is never drawn as a failure.
 */
export function isWaitingForAgentRuntime(
  incident: Incident | null | undefined,
  work: IncidentWorkState | null | undefined,
): boolean {
  if (!incident || !work?.analysis) return false;
  return (
    incident.status === "ANALYZING" &&
    work.analysis.state === "READY" &&
    work.analysis.attempt_count === 0 &&
    work.analysis.worker_id === null
  );
}

/** True while a lease is held and has not yet expired at `now`. */
export function isLeaseActive(
  item: WorkQueueState | null | undefined,
  now: number,
): boolean {
  if (!item?.lease_expires_at) return false;
  const expires = new Date(item.lease_expires_at).getTime();
  return !Number.isNaN(expires) && expires > now;
}
