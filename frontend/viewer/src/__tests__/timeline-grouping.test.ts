import { describe, expect, it } from "vitest";
import {
  collectedWindow,
  collectionPassOf,
  countGroupedEvents,
  entryTimestamp,
  groupTimeline,
  isMilestone,
  UNKNOWN_PASS,
} from "@/lib/timeline-grouping";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";
import type { TimelineEvent } from "@/lib/types";

const adapter = new FixtureViewerAdapter();

function observed(source: string, at: string, collectedAt?: string): TimelineEvent {
  return {
    occurred_at: at,
    stage: "COLLECTION",
    event_type: "EVIDENCE_OBSERVED",
    evidence_ids: [`ev-${source}-${at.slice(-9).replace(/[:.]/g, "")}`],
    details: collectedAt ? { source, collected_at: collectedAt } : { source },
  };
}

function milestone(eventType: string, at: string): TimelineEvent {
  return {
    occurred_at: at,
    stage: "DETECTION",
    event_type: eventType,
    evidence_ids: [],
    details: {},
  };
}

describe("milestone classification", () => {
  it("treats lifecycle and work events as milestones", () => {
    expect(isMilestone(milestone("INCIDENT_CREATED", "2026-08-27T00:00:00Z"))).toBe(true);
    expect(isMilestone(milestone("STATUS_TRANSITIONED", "2026-08-27T00:00:01Z"))).toBe(true);
    expect(isMilestone(milestone("CONTEXT_FROZEN", "2026-08-27T00:00:02Z"))).toBe(true);
    expect(isMilestone(milestone("ALERT_RESOLVED", "2026-08-27T00:00:03Z"))).toBe(true);
    // Backend work events use their own prefixes.
    expect(isMilestone(milestone("INCIDENT_WORK_CLAIMED", "2026-08-27T00:00:04Z"))).toBe(true);
    expect(
      isMilestone(milestone("INCIDENT_LOCALIZATION_WORK_COMPLETED", "2026-08-27T00:00:05Z")),
    ).toBe(true);
  });

  it("never treats a per-Evidence observation as a milestone", () => {
    expect(isMilestone(observed("prometheus", "2026-08-27T00:00:00Z"))).toBe(false);
  });
});

describe("grouping observations by Provider", () => {
  it("folds interleaved Provider observations into one row each", () => {
    // Providers collect concurrently, so their rows interleave by timestamp.
    const timeline: TimelineEvent[] = [
      observed("prometheus", "2026-08-27T00:00:01Z"),
      observed("kubernetes", "2026-08-27T00:00:02Z"),
      observed("prometheus", "2026-08-27T00:00:03Z"),
      observed("kubernetes", "2026-08-27T00:00:04Z"),
      observed("prometheus", "2026-08-27T00:00:05Z"),
      observed("loki", "2026-08-27T00:00:06Z"),
    ];
    const entries = groupTimeline(timeline);
    expect(entries).toHaveLength(3);
    expect(entries.every((entry) => entry.kind === "group")).toBe(true);

    const labels = entries.map((entry) => (entry.kind === "group" ? entry.label : ""));
    expect(labels).toContain("Prometheus observed 3 Evidence items");
    expect(labels).toContain("Kubernetes observed 2 Evidence items");
    expect(labels).toContain("Loki observed 1 Evidence item");
  });

  it("loses no events when grouping", () => {
    const timeline = [
      observed("prometheus", "2026-08-27T00:00:01Z"),
      observed("kubernetes", "2026-08-27T00:00:02Z"),
      milestone("CONTEXT_FROZEN", "2026-08-27T00:00:03Z"),
      observed("prometheus", "2026-08-27T00:00:04Z"),
    ];
    expect(countGroupedEvents(groupTimeline(timeline))).toBe(timeline.length);
  });

  it("keeps milestones as individual rows", () => {
    const timeline = [
      milestone("INCIDENT_CREATED", "2026-08-27T00:00:00Z"),
      observed("prometheus", "2026-08-27T00:00:01Z"),
      observed("prometheus", "2026-08-27T00:00:02Z"),
      milestone("CONTEXT_FROZEN", "2026-08-27T00:00:03Z"),
    ];
    const entries = groupTimeline(timeline);
    const singles = entries.filter((entry) => entry.kind === "event");
    expect(singles).toHaveLength(2);
    expect(singles.map((entry) => (entry.kind === "event" ? entry.event.event_type : ""))).toEqual(
      ["INCIDENT_CREATED", "CONTEXT_FROZEN"],
    );
  });

  it("records the real time span a batch covers", () => {
    const entries = groupTimeline([
      observed("prometheus", "2026-08-27T00:00:01Z"),
      observed("prometheus", "2026-08-27T00:05:00Z"),
    ]);
    const group = entries[0];
    expect(group.kind).toBe("group");
    if (group.kind === "group") {
      expect(group.startedAt).toBe("2026-08-27T00:00:01Z");
      expect(group.endedAt).toBe("2026-08-27T00:05:00Z");
      expect(group.events).toHaveLength(2);
    }
  });

  it("collects every Evidence ID a batch covers", () => {
    const entries = groupTimeline([
      observed("prometheus", "2026-08-27T00:00:01Z"),
      observed("prometheus", "2026-08-27T00:00:02Z"),
    ]);
    const group = entries[0];
    if (group.kind === "group") expect(group.evidenceIds).toHaveLength(2);
  });

  it("keeps entries in chronological order", () => {
    const entries = groupTimeline([
      milestone("INCIDENT_CREATED", "2026-08-27T00:00:00Z"),
      observed("prometheus", "2026-08-27T00:00:01Z"),
      milestone("CONTEXT_FROZEN", "2026-08-27T00:00:09Z"),
    ]);
    const times = entries.map(entryTimestamp);
    expect([...times].sort()).toEqual(times);
  });

  it("separates the same Provider across different stages", () => {
    const entries = groupTimeline([
      observed("prometheus", "2026-08-27T00:00:01Z"),
      { ...observed("prometheus", "2026-08-27T00:00:02Z"), stage: "ANALYSIS" },
    ]);
    expect(entries).toHaveLength(2);
  });
});

describe("grouping a stored Incident timeline", () => {
  it("produces fewer rows than events while covering all of them", async () => {
    const detail = await adapter.getIncidentDetail("inc-checkout-0001");
    const entries = groupTimeline(detail.timeline);
    expect(countGroupedEvents(entries)).toBe(detail.timeline.length);
    expect(entries.length).toBeLessThanOrEqual(detail.timeline.length);
  });

  it("surfaces the lifecycle milestones as their own rows", async () => {
    const detail = await adapter.getIncidentDetail("inc-checkout-0001");
    const entries = groupTimeline(detail.timeline);
    const types = entries
      .filter((entry) => entry.kind === "event")
      .map((entry) => (entry.kind === "event" ? entry.event.event_type : ""));
    expect(types).toContain("INCIDENT_CREATED");
    expect(types).toContain("CONTEXT_FROZEN");
  });
});


describe("observation time is not collection time", () => {
  it("never describes an observation batch as a collection", () => {
    // `occurred_at` is the Evidence `observed_at`: the moment the signal
    // describes, not when a Provider ran.
    const entries = groupTimeline([
      observed("prometheus", "2026-08-27T00:07:14Z"),
      observed("prometheus", "2026-08-27T00:07:44Z"),
    ]);
    const group = entries[0];
    expect(group.kind).toBe("group");
    if (group.kind === "group") {
      expect(group.label).toBe("Prometheus observed 2 Evidence items");
      expect(group.label).not.toMatch(/collect/i);
    }
  });

  it("does not treat an observation predating Incident creation as a collection start", () => {
    // Real shape: signals observed at 00:07 for an Incident created at 00:23.
    const incidentCreatedAt = "2026-08-27T00:23:30Z";
    const entries = groupTimeline([
      observed("prometheus", "2026-08-27T00:07:14Z"),
      observed("prometheus", "2026-08-27T00:07:44Z"),
      milestone("INCIDENT_CREATED", incidentCreatedAt),
    ]);

    const batch = entries.find((entry) => entry.kind === "group");
    expect(batch).toBeDefined();
    if (batch?.kind === "group") {
      // The batch legitimately sorts before creation, so its wording must not
      // claim collection began then.
      expect(batch.startedAt < incidentCreatedAt).toBe(true);
      expect(batch.label).not.toMatch(/collect/i);
    }
  });

  it("reports the real collection window from stored provenance", () => {
    const evidenceById = new Map([
      ["ev-a-001", { provenance: { collected_at: "2026-08-27T00:23:32Z" } }],
      ["ev-a-002", { provenance: { collected_at: "2026-08-27T00:23:35Z" } }],
    ]);
    // Collection ran after the Incident, even though the signals predate it.
    expect(collectedWindow(["ev-a-001", "ev-a-002"], evidenceById)).toEqual({
      start: "2026-08-27T00:23:32Z",
      end: "2026-08-27T00:23:35Z",
    });
  });

  it("returns no collection window when the Evidence is absent from the payload", () => {
    expect(collectedWindow(["ev-missing-1"], new Map())).toBeNull();
  });
});

describe("collection pass identity", () => {
  it("reads the stored collected_at as the pass identifier", () => {
    expect(
      collectionPassOf(observed("prometheus", "2026-08-27T00:05:00Z", "2026-08-27T00:20:00Z")),
    ).toBe("2026-08-27T00:20:00Z");
  });

  it("prefers an explicit attempt identifier when the payload carries one", () => {
    const event = observed("prometheus", "2026-08-27T00:05:00Z", "2026-08-27T00:20:00Z");
    event.details = { ...event.details, collection_attempt: 2 };
    expect(collectionPassOf(event)).toBe("2");
  });

  it("never reads a fenced-write value as pass identity", () => {
    const event = observed("prometheus", "2026-08-27T00:05:00Z");
    event.details = { ...event.details, claim_token: "claim-secret-value" };
    // A token must never become a grouping key.
    expect(collectionPassOf(event)).toBe(UNKNOWN_PASS);
  });

  it("reports an unknown pass for payloads predating the field", () => {
    expect(collectionPassOf(observed("prometheus", "2026-08-27T00:05:00Z"))).toBe(
      UNKNOWN_PASS,
    );
  });
});

describe("collection passes survive API sorting by observed_at", () => {
  /*
   * The failure this guards against:
   *   attempt 1 collects at 00:10 and completes,
   *   attempt 2 runs at 00:20 and reads a metric observed at 00:05,
   *   the API sorts that event before attempt 1's completion.
   * Positional segmentation filed it under attempt 1; pass identity does not.
   */
  const sortedByObservedAt: TimelineEvent[] = [
    // Attempt 2's Evidence, observed earliest, so the API sorts it first.
    observed("prometheus", "2026-08-27T00:05:00Z", "2026-08-27T00:20:00Z"),
    observed("prometheus", "2026-08-27T00:09:00Z", "2026-08-27T00:10:00Z"),
    milestone("COLLECTION_COMPLETED", "2026-08-27T00:10:30Z"),
    observed("prometheus", "2026-08-27T00:19:00Z", "2026-08-27T00:20:00Z"),
  ];

  it("produces two Provider groups despite the misleading order", () => {
    const groups = groupTimeline(sortedByObservedAt).filter((e) => e.kind === "group");
    expect(groups).toHaveLength(2);

    const passes = groups.map((g) => (g.kind === "group" ? g.pass : ""));
    expect(new Set(passes)).toEqual(
      new Set(["2026-08-27T00:10:00Z", "2026-08-27T00:20:00Z"]),
    );
  });

  it("puts the early-observed retry Evidence in the later pass", () => {
    const groups = groupTimeline(sortedByObservedAt).filter((e) => e.kind === "group");
    const second = groups.find(
      (g) => g.kind === "group" && g.pass === "2026-08-27T00:20:00Z",
    );
    expect(second?.kind).toBe("group");
    if (second?.kind === "group") {
      // Both attempt-2 items, including the one observed at 00:05.
      expect(second.events).toHaveLength(2);
      expect(second.events.map((e) => e.occurred_at)).toContain("2026-08-27T00:05:00Z");
    }
  });

  it("loses no event and no Evidence ID", () => {
    const entries = groupTimeline(sortedByObservedAt);
    expect(countGroupedEvents(entries)).toBe(sortedByObservedAt.length);

    const seen = entries.flatMap((entry) =>
      entry.kind === "group" ? entry.evidenceIds : entry.event.evidence_ids,
    );
    const expected = sortedByObservedAt.flatMap((e) => e.evidence_ids);
    expect(seen.sort()).toEqual(expected.sort());
  });

  it("keeps group ids stable and unique", () => {
    const first = groupTimeline(sortedByObservedAt);
    const again = groupTimeline(sortedByObservedAt);
    const ids = first.filter((e) => e.kind === "group").map((e) => e.id);
    expect(ids).toEqual(again.filter((e) => e.kind === "group").map((e) => e.id));
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("keeps observation wording and preserves observed-time ordering", () => {
    const entries = groupTimeline(sortedByObservedAt);
    for (const entry of entries) {
      if (entry.kind === "group") {
        expect(entry.label).toMatch(/observed/);
        expect(entry.label).not.toMatch(/collect/i);
      }
    }
    const times = entries.map(entryTimestamp);
    expect([...times].sort()).toEqual(times);
  });

  it("keeps the real collection window visible per pass", () => {
    const evidenceById = new Map(
      sortedByObservedAt.flatMap((e) =>
        e.evidence_ids.map((id) => [
          id,
          { provenance: { collected_at: String(e.details.collected_at) } },
        ]),
      ),
    );
    const groups = groupTimeline(sortedByObservedAt).filter((e) => e.kind === "group");
    for (const group of groups) {
      if (group.kind !== "group") continue;
      const window = collectedWindow(group.evidenceIds, evidenceById);
      expect(window).not.toBeNull();
      expect(window!.start).toBe(group.pass);
    }
  });

  it("does not split a single pass at an unrelated milestone", () => {
    // One pass, one collected_at: a milestone between items must not split it.
    const entries = groupTimeline([
      observed("prometheus", "2026-08-27T00:01:00Z", "2026-08-27T00:10:00Z"),
      milestone("STATUS_TRANSITIONED", "2026-08-27T00:02:00Z"),
      observed("prometheus", "2026-08-27T00:03:00Z", "2026-08-27T00:10:00Z"),
    ]);
    expect(entries.filter((e) => e.kind === "group")).toHaveLength(1);
  });
});

describe("older payloads without pass identity", () => {
  const legacy: TimelineEvent[] = [
    observed("prometheus", "2026-08-27T00:01:00Z"),
    milestone("COLLECTION_COMPLETED", "2026-08-27T00:02:00Z"),
    observed("prometheus", "2026-08-27T00:03:00Z"),
  ];

  it("groups them into one explicitly unknown pass", () => {
    const groups = groupTimeline(legacy).filter((e) => e.kind === "group");
    expect(groups).toHaveLength(1);
    if (groups[0].kind === "group") {
      expect(groups[0].pass).toBe(UNKNOWN_PASS);
      // It must not claim retries were separated when they were not.
      expect(groups[0].passKnown).toBe(false);
      expect(groups[0].events).toHaveLength(2);
    }
  });

  it("still loses no event", () => {
    expect(countGroupedEvents(groupTimeline(legacy))).toBe(legacy.length);
  });

  it("marks a known pass as known", () => {
    const groups = groupTimeline([
      observed("prometheus", "2026-08-27T00:01:00Z", "2026-08-27T00:10:00Z"),
    ]).filter((e) => e.kind === "group");
    if (groups[0].kind === "group") expect(groups[0].passKnown).toBe(true);
  });
});
