import { beforeEach, describe, expect, it } from "vitest";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";
import { reportRequirementGaps } from "@/lib/report-refs";
import type { EntityRef, RcaReport } from "@/lib/types";

let report: RcaReport;

beforeEach(async () => {
  const detail = await new FixtureViewerAdapter().getIncidentDetail("inc-checkout-0001");
  report = structuredClone(detail.reports[0].report);
  report.root_cause!.cause_id = "kubernetes.missing-configmap";
  report.hypotheses[0].cause_id = "kubernetes.missing-configmap";
  report.hypotheses[1].missing_evidence = ["alternative proof", "alternative proof"];
  report.hypotheses[2].missing_evidence = ["alternative proof", "another requirement"];
});

describe("Report requirement gaps", () => {
  it("deduplicates within each group without changing stored conclusions", () => {
    report.hypotheses[0].missing_evidence = ["selected proof", "selected proof"];
    const before = structuredClone(report);
    expect(reportRequirementGaps(report)).toEqual({
      selected: ["selected proof"], other: ["alternative proof", "another requirement"],
    });
    expect(report).toEqual(before);
  });

  it("matches cause and entity, not rank or array position", () => {
    report.hypotheses[0].rank = 3;
    report.hypotheses.reverse();
    expect(reportRequirementGaps(report).selected).toEqual([]);
    report.hypotheses[2].cause_id = "kubernetes.image-pull-failure";
    expect(reportRequirementGaps(report).selected).toBeNull();
  });

  it.each([
    { cluster_id: "other-cluster" }, { uid: "replacement-uid" },
    { namespace: "other-namespace" }, { kind: "Pod" }, { name: "other-name" },
    { api_version: "other/v1" },
  ])("does not match a different Kubernetes reference: %j", (difference) => {
    report.hypotheses[0].entity = { ...report.hypotheses[0].entity, ...difference } as EntityRef;
    expect(reportRequirementGaps(report).selected).toBeNull();
  });

  it("matches graph identity but never falls back to a shared display name", () => {
    const entity: EntityRef = {
      entity_id: "entity-fixture", entity_type: "k8s.pod", domain: "kubernetes",
      name: "same-name", scope: { cluster_id: "fixture-cluster" },
      external_ref: "fixture-uid", exists: true,
    };
    report.root_cause!.entity = entity;
    report.hypotheses[0].entity = { ...entity };
    expect(reportRequirementGaps(report).selected).toEqual([]);
    report.hypotheses[0].entity = { ...entity, entity_id: "other-entity" };
    expect(reportRequirementGaps(report).selected).toBeNull();
  });

  it("leaves selected requirements unknown for legacy or unmatched Reports", () => {
    delete report.root_cause!.cause_id;
    expect(reportRequirementGaps(report).selected).toBeNull();
  });

  it("keeps ABSTAIN requirements unselected, even with a ranked hypothesis", () => {
    report.root_cause = null;
    report.status = "inconclusive";
    report.hypotheses[0].missing_evidence = ["selected proof"];
    expect(reportRequirementGaps(report)).toEqual({
      selected: null, other: ["selected proof", "alternative proof", "another requirement"],
    });
    expect(report.status).toBe("inconclusive");
    expect(report.root_cause).toBeNull();
  });
});
