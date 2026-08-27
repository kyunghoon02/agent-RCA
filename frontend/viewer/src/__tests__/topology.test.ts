import { describe, expect, it } from "vitest";
import { summariseTopology } from "@/lib/topology";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";
import type { ContextPackage, KubernetesEntityRef, StatePath } from "@/lib/types";

function entity(kind: string, name: string): KubernetesEntityRef {
  return {
    api_version: "v1",
    cluster_id: "cluster-a",
    kind,
    namespace: "online-boutique",
    name,
    uid: `uid-${kind}-${name}`,
    exists: true,
  };
}

function path(
  id: string,
  kinds: string[],
  evidenceIds: string[],
  relations?: string[],
): StatePath {
  return {
    path_id: id,
    entities: kinds.map((kind, index) => entity(kind, `res-${index}`)),
    relations: relations ?? kinds.slice(1).map(() => "OWNS"),
    evidence_ids: evidenceIds,
  };
}

function context(paths: StatePath[], evidenceIds: string[]): ContextPackage {
  return {
    schema_version: "1.0.0",
    context_id: "ctx-topology-0001",
    incident_id: "inc-topology-0001",
    frozen_at: "2026-08-27T00:00:00Z",
    source_entity: entity("Service", "checkoutservice"),
    scope: {
      incident_id: "inc-topology-0001",
      seed_entity_ids: ["ent-aaaaaaaaaaaaaaaa"],
      domains: ["kubernetes"],
      correlation_keys: { namespace: "online-boutique" },
      relation_types: ["OWNS"],
      time_window: { start: "2026-08-27T00:00:00Z", end: "2026-08-27T00:10:00Z" },
      max_entities: 60,
      max_depth: 4,
    },
    state_paths: paths,
    evidence_ids: evidenceIds,
    recent_change_evidence_ids: [],
    missing_evidence: [],
    collector_failures: [],
    localization: {
      strategy: "stategraph",
      candidate_entities_before: 100,
      candidate_entities_after: paths.length,
      context_completeness: 1,
    },
  };
}

describe("unique Evidence per topology shape", () => {
  it("counts an Evidence ID once even when many paths of a shape reference it", () => {
    // Overlapping paths are the norm: the same Pod Evidence appears on every
    // path that traverses that Pod.
    const shared = ["ev-shared-00000001", "ev-shared-00000002"];
    const summary = summariseTopology(
      context(
        [
          path("p1", ["Service", "Pod"], shared),
          path("p2", ["Service", "Pod"], shared),
          path("p3", ["Service", "Pod"], shared),
        ],
        shared,
      ),
    );

    expect(summary.shapes).toHaveLength(1);
    expect(summary.shapes[0].count).toBe(3);
    // Summing references would have produced 6.
    expect(summary.shapes[0].uniqueEvidenceCount).toBe(2);
  });

  it("unions distinct Evidence IDs across paths of the same shape", () => {
    const summary = summariseTopology(
      context(
        [
          path("p1", ["Service", "Pod"], ["ev-aaaaaaaa0001", "ev-bbbbbbbb0002"]),
          path("p2", ["Service", "Pod"], ["ev-bbbbbbbb0002", "ev-cccccccc0003"]),
        ],
        ["ev-aaaaaaaa0001", "ev-bbbbbbbb0002", "ev-cccccccc0003"],
      ),
    );
    expect(summary.shapes[0].uniqueEvidenceCount).toBe(3);
  });

  it("keeps shapes independent of one another", () => {
    const summary = summariseTopology(
      context(
        [
          path("p1", ["Service", "Pod"], ["ev-aaaaaaaa0001"]),
          path("p2", ["Service", "Pod", "Node"], ["ev-aaaaaaaa0001", "ev-dddddddd0004"]),
        ],
        ["ev-aaaaaaaa0001", "ev-dddddddd0004"],
      ),
    );
    const byLength = [...summary.shapes].sort((a, b) => a.types.length - b.types.length);
    expect(byLength[0].uniqueEvidenceCount).toBe(1);
    expect(byLength[1].uniqueEvidenceCount).toBe(2);
  });

  it("reports zero for a shape whose paths carry no Evidence", () => {
    const summary = summariseTopology(context([path("p1", ["Service"], [])], ["ev-x0000001"]));
    expect(summary.shapes[0].uniqueEvidenceCount).toBe(0);
  });
});

describe("Context-wide Evidence count", () => {
  it("stays based on context.evidence_ids, not on path references", () => {
    const summary = summariseTopology(
      context(
        [
          path("p1", ["Service", "Pod"], ["ev-aaaaaaaa0001", "ev-aaaaaaaa0001"]),
          path("p2", ["Service", "Pod"], ["ev-aaaaaaaa0001"]),
        ],
        ["ev-aaaaaaaa0001", "ev-bbbbbbbb0002", "ev-cccccccc0003"],
      ),
    );
    // Three IDs are frozen into the Context even though the paths cite one.
    expect(summary.evidenceCount).toBe(3);
    expect(summary.shapes[0].uniqueEvidenceCount).toBe(1);
  });

  it("never exceeds the Context Evidence count on a stored Incident", async () => {
    const adapter = new FixtureViewerAdapter();
    const detail = await adapter.getIncidentDetail("inc-checkout-0001");
    const summary = summariseTopology(detail.contexts[0]);
    for (const shape of summary.shapes) {
      expect(shape.uniqueEvidenceCount).toBeLessThanOrEqual(summary.evidenceCount);
    }
  });
});


describe("topology shape identity includes relations", () => {
  it("separates identical entity types joined by different relations", () => {
    // Service --SELECTS--> Pod is not Service --DEPENDS_ON--> Pod.
    const summary = summariseTopology(
      context(
        [
          path("p1", ["Service", "Pod"], ["ev-aaaaaaaa0001"], ["SELECTS"]),
          path("p2", ["Service", "Pod"], ["ev-bbbbbbbb0002"], ["DEPENDS_ON"]),
        ],
        ["ev-aaaaaaaa0001", "ev-bbbbbbbb0002"],
      ),
    );

    expect(summary.shapes).toHaveLength(2);
    expect(summary.shapes.map((shape) => shape.relations)).toEqual(
      expect.arrayContaining([["SELECTS"], ["DEPENDS_ON"]]),
    );
    expect(summary.shapes.every((shape) => shape.count === 1)).toBe(true);
  });

  it("still merges paths that match on both types and relations", () => {
    const summary = summariseTopology(
      context(
        [
          path("p1", ["Service", "Pod"], ["ev-aaaaaaaa0001"], ["SELECTS"]),
          path("p2", ["Service", "Pod"], ["ev-bbbbbbbb0002"], ["SELECTS"]),
        ],
        ["ev-aaaaaaaa0001", "ev-bbbbbbbb0002"],
      ),
    );
    expect(summary.shapes).toHaveLength(1);
    expect(summary.shapes[0].count).toBe(2);
    expect(summary.shapes[0].uniqueEvidenceCount).toBe(2);
  });

  it("separates longer chains that diverge only at one relation", () => {
    const summary = summariseTopology(
      context(
        [
          path("p1", ["Service", "Pod", "Node"], ["ev-aaaaaaaa0001"], ["SELECTS", "SCHEDULED_ON"]),
          path("p2", ["Service", "Pod", "Node"], ["ev-bbbbbbbb0002"], ["SELECTS", "RUNS_ON"]),
        ],
        ["ev-aaaaaaaa0001", "ev-bbbbbbbb0002"],
      ),
    );
    expect(summary.shapes).toHaveLength(2);
  });

  it("keeps deduplication scoped to the exact shape", () => {
    const shared = ["ev-shared-00000001"];
    const summary = summariseTopology(
      context(
        [
          path("p1", ["Service", "Pod"], shared, ["SELECTS"]),
          path("p2", ["Service", "Pod"], shared, ["SELECTS"]),
          path("p3", ["Service", "Pod"], shared, ["DEPENDS_ON"]),
        ],
        shared,
      ),
    );
    expect(summary.shapes).toHaveLength(2);
    for (const shape of summary.shapes) {
      expect(shape.uniqueEvidenceCount).toBe(1);
    }
  });

  it("gives one relation per edge so the display holds for every member path", () => {
    const summary = summariseTopology(
      context([path("p1", ["Service", "Pod", "Node"], [], ["SELECTS"])], ["ev-x0000001"]),
    );
    const shape = summary.shapes[0];
    // A missing relation is filled rather than left undefined.
    expect(shape.relations).toHaveLength(shape.types.length - 1);
    expect(shape.relations[1]).toBe("RELATED");
  });
});
