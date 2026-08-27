import { describe, expect, it } from "vitest";
import {
  buildRelevanceIndex,
  groupEvidenceBySubject,
  isDegraded,
  relevanceTags,
  resultStatus,
  subjectKey,
  summariseEvidence,
} from "@/lib/evidence-grouping";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";
import type { EvidenceItem, KubernetesEntityRef } from "@/lib/types";

const adapter = new FixtureViewerAdapter();

async function load(incidentId: string) {
  const detail = await adapter.getIncidentDetail(incidentId);
  const index = buildRelevanceIndex(detail.evidence, detail.contexts, detail.reports);
  return { detail, index, groups: groupEvidenceBySubject(detail.evidence, index) };
}

function pod(name: string, uid: string | null): KubernetesEntityRef {
  return {
    api_version: "v1",
    cluster_id: "agent-rca-dev",
    kind: "Pod",
    namespace: "online-boutique",
    name,
    uid,
    exists: true,
  };
}

describe("subject identity", () => {
  it("separates two Pods that share a name but differ by UID", () => {
    // A restarted Pod reuses its name; merging the two would fuse unrelated
    // observations into one signal cluster.
    expect(subjectKey(pod("checkoutservice-abc", "uid-1"))).not.toBe(
      subjectKey(pod("checkoutservice-abc", "uid-2")),
    );
  });

  it("keys on cluster, namespace, kind, name and UID together", () => {
    const key = subjectKey(pod("checkoutservice-abc", "uid-1"));
    expect(key).toContain("agent-rca-dev");
    expect(key).toContain("online-boutique");
    expect(key).toContain("Pod");
    expect(key).toContain("checkoutservice-abc");
    expect(key).toContain("uid-1");
  });

  it("gives the same subject the same key across separate Evidence items", () => {
    expect(subjectKey(pod("p", "u"))).toBe(subjectKey(pod("p", "u")));
  });
});

describe("grouping Evidence by subject", () => {
  it("collapses many Evidence items into one row per observed subject", async () => {
    const { detail, groups } = await load("inc-checkout-0001");
    expect(detail.evidence.length).toBeGreaterThan(groups.length);
    const total = groups.reduce((sum, group) => sum + group.items.length, 0);
    // Grouping must never lose or duplicate an item.
    expect(total).toBe(detail.evidence.length);
  });

  it("gathers every signal for one Pod into a single group", async () => {
    const { groups } = await load("inc-checkout-0001");
    const podGroup = groups.find((group) => group.identity.kind === "Pod");
    expect(podGroup).toBeDefined();
    expect(podGroup!.items.length).toBeGreaterThan(1);
    // The signal cluster spans more than one collector.
    expect(podGroup!.sources.length).toBeGreaterThanOrEqual(1);
  });

  it("orders the most-cited subject first", async () => {
    const { groups } = await load("inc-checkout-0001");
    const cited = groups.map((group) => group.citedCount);
    expect([...cited].sort((a, b) => b - a)).toEqual(cited);
  });

  it("counts context membership, recent change and degraded per group", async () => {
    const { groups } = await load("inc-rediscart-0004");
    const totals = groups.reduce(
      (acc, group) => ({
        inContext: acc.inContext + group.inContextCount,
        degraded: acc.degraded + group.degradedCount,
      }),
      { inContext: 0, degraded: 0 },
    );
    expect(totals.inContext).toBeGreaterThan(0);
    expect(totals.degraded).toBeGreaterThan(0);
  });

  it("sorts items inside a group chronologically", async () => {
    const { groups } = await load("inc-checkout-0001");
    for (const group of groups) {
      const times = group.items.map((item) => item.observed_at);
      expect([...times].sort()).toEqual(times);
    }
  });
});

describe("relevance comes only from stored references", () => {
  it("tags Evidence cited by a Report", async () => {
    const { detail, index } = await load("inc-checkout-0001");
    const cited = detail.reports[0].report.root_cause!.supporting_evidence_ids[0];
    expect(relevanceTags(cited, index)).toContain("CITED_BY_REPORT");
  });

  it("tags Evidence the Report did not use", async () => {
    const { detail, index } = await load("inc-checkout-0001");
    const uncited = detail.evidence
      .map((item) => item.evidence_id)
      .find((id) => !index.citedByReport.has(id));
    expect(uncited).toBeDefined();
    expect(relevanceTags(uncited!, index)).toContain("NOT_USED_BY_REPORT");
  });

  it("tags Evidence outside the Frozen Context", async () => {
    const { detail, index } = await load("inc-rediscart-0004");
    const outside = detail.evidence
      .map((item) => item.evidence_id)
      .find((id) => !index.inContext.has(id));
    expect(outside).toBeDefined();
    expect(relevanceTags(outside!, index)).toContain("OUTSIDE_CONTEXT");
  });

  it("adds no Report tags when no Report exists", async () => {
    const { detail, index } = await load("inc-cartservice-0002");
    expect(index.hasReport).toBe(false);
    for (const item of detail.evidence) {
      const tags = relevanceTags(item.evidence_id, index);
      expect(tags).not.toContain("CITED_BY_REPORT");
      expect(tags).not.toContain("NOT_USED_BY_REPORT");
    }
  });

  it("records Context references the payload did not return", async () => {
    const { detail, index } = await load("inc-checkout-0001");
    void detail;
    expect(index.missingFromPayload.size).toBe(0);
  });
});

describe("collector result status", () => {
  it("reads the verdict from either result_status or status", () => {
    const base = {
      schema_version: "1.0.0",
      evidence_id: "ev-test-0001",
      incident_id: "inc-test-0001",
      source: "prometheus",
      kind: "metric-summary",
      observed_at: "2026-08-27T00:00:00Z",
      window: { start: "2026-08-27T00:00:00Z", end: "2026-08-27T00:00:00Z" },
      subject: pod("p", "u"),
      summary: "s",
      provenance: {
        provider: "p",
        query: "q",
        locator: "l",
        collected_at: "2026-08-27T00:00:00Z",
        content_hash: `sha256:${"0".repeat(64)}`,
      },
      quality: { freshness: "live" as const, completeness: 1, confidence: 1 },
      redactions: [],
    };
    const live = { ...base, facts: { result_status: "HAS_DATA" } } as EvidenceItem;
    const legacy = { ...base, facts: { status: "INSUFFICIENT_DATA" } } as EvidenceItem;
    expect(resultStatus(live)).toBe("HAS_DATA");
    expect(resultStatus(legacy)).toBe("INSUFFICIENT_DATA");
    expect(isDegraded(live)).toBe(false);
    expect(isDegraded(legacy)).toBe(true);
  });
});

describe("evidence summary", () => {
  it("counts context membership, degradation and subjects", async () => {
    const { detail, index, groups } = await load("inc-rediscart-0004");
    const summary = summariseEvidence(detail.evidence, index, detail.contexts, groups);
    expect(summary.total).toBe(detail.evidence.length);
    expect(summary.subjects).toBe(groups.length);
    expect(summary.inContext + summary.outsideContext).toBe(summary.total);
    expect(summary.providerFailures).toBeGreaterThan(0);
  });
});
