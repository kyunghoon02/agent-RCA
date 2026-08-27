import { describe, expect, it } from "vitest";
import {
  groupRepeatedIncidents,
  hasGroupableRepeats,
  hasStableEntityIdentity,
  reportAvailability,
  summariseRepeatGroup,
} from "@/lib/incident-list";
import type {
  EntityRef,
  GraphEntityRef,
  IncidentStatus,
  IncidentSummary,
  KubernetesEntityRef,
} from "@/lib/types";

function k8s(overrides: Partial<KubernetesEntityRef> = {}): KubernetesEntityRef {
  return {
    api_version: "v1",
    cluster_id: "cluster-a",
    kind: "Pod",
    namespace: "online-boutique",
    name: "checkoutservice",
    uid: "uid-1",
    exists: true,
    ...overrides,
  };
}

function graph(overrides: Partial<GraphEntityRef> = {}): GraphEntityRef {
  return {
    entity_id: "ent-aaaaaaaaaaaaaaaa",
    entity_type: "Service",
    domain: "kubernetes",
    name: "checkoutservice",
    scope: { namespace: "online-boutique" },
    external_ref: null,
    exists: true,
    ...overrides,
  };
}

let sequence = 0;
function incident(
  entity: EntityRef,
  overrides: Partial<IncidentSummary> = {},
): IncidentSummary {
  sequence += 1;
  return {
    incident_id: `inc-generated-${String(sequence).padStart(8, "0")}`,
    status: "ANALYZING",
    severity: "critical",
    source: "alertmanager",
    triggered_at: "2026-08-27T00:00:00Z",
    updated_at: "2026-08-27T00:00:00Z",
    alert_name: "AgentRCAControlledCheckoutOOM",
    source_entity: entity,
    collector_problem_count: 0,
    ...overrides,
  };
}

describe("collector problem aggregation", () => {
  it("sums collector problems across every member of a repeat group", () => {
    const items = [
      incident(k8s(), { collector_problem_count: 2 }),
      incident(k8s(), { collector_problem_count: 3 }),
      incident(k8s(), { collector_problem_count: 4 }),
    ];
    // The group total is the sum, never the number of repeats.
    expect(summariseRepeatGroup(items).collectorProblemTotal).toBe(9);
    expect(summariseRepeatGroup(items).total).toBe(3);
  });

  it("reports zero when no member has a collector problem", () => {
    const items = [incident(k8s()), incident(k8s()), incident(k8s())];
    const summary = summariseRepeatGroup(items);
    expect(summary.collectorProblemTotal).toBe(0);
    // Repeat count and problem count are independent facts.
    expect(summary.total).toBe(3);
  });

  it("keeps the sum distinct from the repeat count when they could coincide", () => {
    const items = [
      incident(k8s(), { collector_problem_count: 1 }),
      incident(k8s(), { collector_problem_count: 1 }),
    ];
    const summary = summariseRepeatGroup(items);
    expect(summary.collectorProblemTotal).toBe(2);
    expect(summary.total).toBe(2);
  });
});

describe("entity identity collisions", () => {
  it("does not merge the same name and namespace across different clusters", () => {
    const rows = groupRepeatedIncidents([
      incident(k8s({ cluster_id: "cluster-a", uid: "uid-1" })),
      incident(k8s({ cluster_id: "cluster-b", uid: "uid-1" })),
    ]);
    expect(rows).toHaveLength(2);
    expect(rows.every((row) => row.kind === "single")).toBe(true);
  });

  it("does not merge different Kubernetes kinds that share a name", () => {
    const rows = groupRepeatedIncidents([
      incident(k8s({ kind: "Pod", uid: "uid-1" })),
      incident(k8s({ kind: "Service", uid: "uid-1" })),
    ]);
    expect(rows).toHaveLength(2);
  });

  it("does not merge different UIDs that share cluster, kind, namespace and name", () => {
    // A recreated resource reuses namespace and name but gets a new UID.
    const rows = groupRepeatedIncidents([
      incident(k8s({ uid: "uid-1" })),
      incident(k8s({ uid: "uid-2" })),
    ]);
    expect(rows).toHaveLength(2);
  });

  it("keeps Incidents separate when no stable UID is available", () => {
    const rows = groupRepeatedIncidents([
      incident(k8s({ uid: null })),
      incident(k8s({ uid: null })),
      incident(k8s({ uid: null })),
    ]);
    expect(rows).toHaveLength(3);
    expect(rows.every((row) => row.kind === "single")).toBe(true);
  });

  it("keeps Incidents separate when no cluster_id is available", () => {
    const rows = groupRepeatedIncidents([
      incident(k8s({ cluster_id: undefined })),
      incident(k8s({ cluster_id: undefined })),
    ]);
    expect(rows).toHaveLength(2);
  });

  it("groups Kubernetes entities that share cluster, kind and UID", () => {
    const rows = groupRepeatedIncidents([incident(k8s()), incident(k8s())]);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("repeat");
  });

  it("groups graph entities that share entity_id, domain and type", () => {
    const rows = groupRepeatedIncidents([incident(graph()), incident(graph())]);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("repeat");
  });

  it("does not merge graph entities with different entity_id", () => {
    const rows = groupRepeatedIncidents([
      incident(graph({ entity_id: "ent-aaaaaaaaaaaaaaaa" })),
      incident(graph({ entity_id: "ent-bbbbbbbbbbbbbbbb" })),
    ]);
    expect(rows).toHaveLength(2);
  });

  it("does not merge graph entities from different domains", () => {
    const rows = groupRepeatedIncidents([
      incident(graph({ domain: "kubernetes" })),
      incident(graph({ domain: "web-service" })),
    ]);
    expect(rows).toHaveLength(2);
  });

  it("does not merge different alert sources", () => {
    const rows = groupRepeatedIncidents([
      incident(k8s(), { source: "alertmanager" }),
      incident(k8s(), { source: "cloud-monitoring" }),
    ]);
    expect(rows).toHaveLength(2);
  });

  it("does not merge different alert names or severities", () => {
    expect(
      groupRepeatedIncidents([
        incident(k8s(), { alert_name: "A" }),
        incident(k8s(), { alert_name: "B" }),
      ]),
    ).toHaveLength(2);
    expect(
      groupRepeatedIncidents([
        incident(k8s(), { severity: "critical" }),
        incident(k8s(), { severity: "warning" }),
      ]),
    ).toHaveLength(2);
  });
});

describe("grouping preserves every Incident", () => {
  it("keeps consecutive-only behaviour", () => {
    const rows = groupRepeatedIncidents([
      incident(k8s({ uid: "uid-1" })),
      incident(k8s({ uid: "uid-2" })),
      incident(k8s({ uid: "uid-1" })),
    ]);
    // The two uid-1 Incidents are not adjacent, so they must not be folded.
    expect(rows).toHaveLength(3);
  });

  it("discards no Incident when folding", () => {
    const items = [
      incident(k8s()),
      incident(k8s()),
      incident(k8s({ uid: "uid-9" })),
      incident(k8s({ uid: "uid-9" })),
    ];
    const rows = groupRepeatedIncidents(items);
    const flattened = rows.flatMap((row) =>
      row.kind === "repeat" ? row.items : [row.item],
    );
    expect(flattened.map((i) => i.incident_id)).toEqual(items.map((i) => i.incident_id));
  });
});

describe("mixed-status repeat groups", () => {
  const statuses: IncidentStatus[] = [
    "REPORTED",
    "ANALYZING",
    "ANALYZING",
    "ANALYZING",
    "ANALYZING",
    "ANALYZING",
    "FAILED",
    "FAILED",
    "FAILED",
  ];

  it("flags a group spanning several lifecycle statuses as mixed", () => {
    // Mirrors a real controlled-fault run: 1 REPORTED, 5 ANALYZING, 3 FAILED.
    const items = statuses.map((status) => incident(k8s(), { status }));
    const summary = summariseRepeatGroup(items);

    expect(summary.isMixed).toBe(true);
    expect(summary.total).toBe(9);
    expect(summary.statusCounts).toEqual([
      { status: "ANALYZING", count: 5 },
      { status: "FAILED", count: 3 },
      { status: "REPORTED", count: 1 },
    ]);
  });

  it("counts report-available Incidents rather than trusting the newest member", () => {
    const items = statuses.map((status) => incident(k8s(), { status }));
    const summary = summariseRepeatGroup(items);
    // The newest member is REPORTED, but only one of nine has a Report.
    expect(summary.reportAvailableCount).toBe(1);
    expect(reportAvailability(items[0].status)).toBe("AVAILABLE");
  });

  it("does not flag a homogeneous group as mixed", () => {
    const items = [
      incident(k8s(), { status: "ANALYZING" }),
      incident(k8s(), { status: "ANALYZING" }),
    ];
    const summary = summariseRepeatGroup(items);
    expect(summary.isMixed).toBe(false);
    expect(summary.statusCounts).toEqual([{ status: "ANALYZING", count: 2 }]);
    expect(summary.reportAvailableCount).toBe(0);
  });

  it("counts every REPORTED member of an all-reported group", () => {
    const items = [
      incident(k8s(), { status: "REPORTED" }),
      incident(k8s(), { status: "REPORTED" }),
    ];
    const summary = summariseRepeatGroup(items);
    expect(summary.isMixed).toBe(false);
    expect(summary.reportAvailableCount).toBe(2);
  });
});


describe("page groupability", () => {
  it("reports a page groupable when a stable Kubernetes identity repeats", () => {
    expect(hasGroupableRepeats([incident(k8s()), incident(k8s())])).toBe(true);
  });

  it("reports a page groupable when a stable graph identity repeats", () => {
    expect(hasGroupableRepeats([incident(graph()), incident(graph())])).toBe(true);
  });

  it("reports a live-shaped page with uid=null as not groupable", () => {
    // The live list shape: Kubernetes entity, no cluster_id, null uid.
    const live = { ...k8s({ uid: null }), cluster_id: undefined };
    expect(hasGroupableRepeats([incident(live), incident(live), incident(live)])).toBe(
      false,
    );
  });

  it("reports a page of distinct entities as not groupable", () => {
    expect(
      hasGroupableRepeats([
        incident(k8s({ uid: "uid-1" })),
        incident(k8s({ uid: "uid-2" })),
      ]),
    ).toBe(false);
  });

  it("reports the same name in different clusters as not groupable", () => {
    expect(
      hasGroupableRepeats([
        incident(k8s({ cluster_id: "cluster-a" })),
        incident(k8s({ cluster_id: "cluster-b" })),
      ]),
    ).toBe(false);
  });

  it("reports an empty page as not groupable", () => {
    expect(hasGroupableRepeats([])).toBe(false);
  });
});

describe("stable identity detection", () => {
  it("accepts a Kubernetes entity with cluster_id and uid", () => {
    expect(hasStableEntityIdentity(incident(k8s()))).toBe(true);
  });

  it("accepts a graph entity", () => {
    expect(hasStableEntityIdentity(incident(graph()))).toBe(true);
  });

  it("rejects a Kubernetes entity missing uid", () => {
    expect(hasStableEntityIdentity(incident(k8s({ uid: null })))).toBe(false);
  });

  it("rejects a Kubernetes entity missing cluster_id", () => {
    expect(hasStableEntityIdentity(incident(k8s({ cluster_id: undefined })))).toBe(false);
  });
});
