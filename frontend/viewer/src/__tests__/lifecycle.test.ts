import { describe, expect, it } from "vitest";
import {
  deriveLifecycle,
  LIFECYCLE_STEPS,
  reachedStatusesFromTimeline,
} from "@/lib/lifecycle";
import type { TimelineEvent } from "@/lib/types";

function transition(to: string): TimelineEvent {
  return {
    occurred_at: "2026-07-27T01:00:00Z",
    stage: "COLLECTION",
    event_type: "STATUS_TRANSITIONED",
    evidence_ids: [],
    details: { from: "RECEIVED", to },
  };
}

describe("deriveLifecycle", () => {
  it("marks every earlier step complete and the current status in progress", () => {
    const steps = deriveLifecycle("LOCALIZING");
    expect(steps.map((step) => step.state)).toEqual([
      "complete",
      "complete",
      "current",
      "pending",
      "pending",
    ]);
  });

  it("treats REPORTED as a completed terminal step", () => {
    const steps = deriveLifecycle("REPORTED");
    expect(steps.every((step) => step.state === "complete")).toBe(true);
  });

  it("stops a FAILED Incident at the last step the timeline actually recorded", () => {
    const steps = deriveLifecycle("FAILED", ["RECEIVED", "COLLECTING"]);
    expect(steps.map((step) => ({ status: step.status, state: step.state }))).toEqual([
      { status: "RECEIVED", state: "complete" },
      { status: "COLLECTING", state: "failed" },
      { status: "LOCALIZING", state: "not-reached" },
      { status: "ANALYZING", state: "not-reached" },
      { status: "REPORTED", state: "not-reached" },
    ]);
  });

  it("does not claim progress for a PARTIAL Incident with no recorded transitions", () => {
    const steps = deriveLifecycle("PARTIAL");
    expect(steps[0].state).toBe("failed");
    // Later steps must read as never reached, never as still pending, so the
    // run is not shown as having skipped ahead.
    expect(steps.slice(1).every((step) => step.state === "not-reached")).toBe(true);
  });

  it("never reports a step as skipped: no pending step precedes a complete one", () => {
    for (const status of LIFECYCLE_STEPS) {
      const steps = deriveLifecycle(status);
      const firstPending = steps.findIndex((step) => step.state === "pending");
      const lastComplete = steps.map((step) => step.state).lastIndexOf("complete");
      if (firstPending !== -1 && lastComplete !== -1) {
        expect(firstPending).toBeGreaterThan(lastComplete);
      }
    }
  });
});

describe("reachedStatusesFromTimeline", () => {
  it("collects transitions in lifecycle order and always includes RECEIVED", () => {
    const timeline: TimelineEvent[] = [
      {
        occurred_at: "2026-07-27T00:59:00Z",
        stage: "DETECTION",
        event_type: "INCIDENT_CREATED",
        evidence_ids: [],
        details: {},
      },
      transition("LOCALIZING"),
      transition("COLLECTING"),
    ];
    expect(reachedStatusesFromTimeline(timeline)).toEqual([
      "RECEIVED",
      "COLLECTING",
      "LOCALIZING",
    ]);
  });

  it("ignores transitions to non-lifecycle outcomes", () => {
    expect(reachedStatusesFromTimeline([transition("FAILED")])).toEqual([]);
  });
});
