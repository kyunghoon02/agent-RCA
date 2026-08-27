import { describe, expect, it } from "vitest";
import {
  controlledVerificationId,
  deriveDiagnosis,
  isUnclaimedAnalysis,
  outcomeFromReport,
  PARTIAL_WITH_ROOT_CAUSE,
  PARTIAL_WITHOUT_ROOT_CAUSE,
} from "@/lib/diagnosis";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";

const adapter = new FixtureViewerAdapter();

async function load(incidentId: string) {
  const [detail, work] = await Promise.all([
    adapter.getIncidentDetail(incidentId),
    adapter.getIncidentWorkState(incidentId),
  ]);
  return { detail, work };
}

describe("RCA outcome is not pipeline status", () => {
  it("reports NOT_AVAILABLE while the pipeline is ANALYZING with no Report", async () => {
    const { detail, work } = await load("inc-cartservice-0002");
    expect(detail.incident.status).toBe("ANALYZING");
    expect(detail.reports).toHaveLength(0);

    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.outcome).toBe("NOT_AVAILABLE");
  });

  it("never derives an outcome from Incident status alone", async () => {
    const { detail, work } = await load("inc-checkout-0001");
    const withoutReports = deriveDiagnosis(detail.incident, work, []);
    // REPORTED status, but the Report list is what carries the conclusion.
    expect(detail.incident.status).toBe("REPORTED");
    expect(withoutReports.outcome).toBe("NOT_AVAILABLE");
  });

  it("maps a conclusive Report to PROVEN", async () => {
    const { detail, work } = await load("inc-checkout-0001");
    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.outcome).toBe("PROVEN");
    expect(diagnosis.state).toBe("PROVEN");
  });

  it("maps an inconclusive Report with no root cause to ABSTAIN", async () => {
    const { detail, work } = await load("inc-frontend-0003");
    expect(detail.reports[0].report.root_cause).toBeNull();
    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.outcome).toBe("ABSTAIN");
    // An abstention is a safe result, so it must not be styled as a failure.
    expect(diagnosis.tone).not.toBe("failed");
  });

  it("maps a partial Report to PARTIAL", async () => {
    const { detail, work } = await load("inc-emailsvc-0008");
    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.outcome).toBe("PARTIAL");
  });

  it("distinguishes AMBIGUOUS from ABSTAIN by whether a root cause was recorded", async () => {
    const { detail } = await load("inc-frontend-0003");
    const bundle = detail.reports[0];
    expect(outcomeFromReport(bundle)).toBe("ABSTAIN");

    const withRootCause = {
      ...bundle,
      report: {
        ...bundle.report,
        root_cause: {
          summary: "candidate",
          entity: bundle.report.hypotheses[0].entity,
          supporting_evidence_ids: ["ev-frontend-5xx-01"],
          reference_document_ids: [],
        },
      },
    };
    expect(outcomeFromReport(withRootCause)).toBe("AMBIGUOUS");
  });
});

describe("Waiting-for-Agent state", () => {
  it("detects an analysis item that is READY and never claimed", async () => {
    const { work } = await load("inc-cartservice-0002");
    expect(isUnclaimedAnalysis(work.analysis)).toBe(true);
  });

  it("produces the required title and description", async () => {
    const { detail, work } = await load("inc-cartservice-0002");
    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);

    expect(diagnosis.state).toBe("WAITING_AGENT");
    expect(diagnosis.title).toBe("Waiting for Agent runtime");
    expect(diagnosis.description).toBe(
      "The Frozen Context is ready and pinned, but no Agent runtime has claimed this analysis work.",
    );
    expect(diagnosis.awaitingAgentRuntime).toBe(true);
  });

  it("is a waiting state, never a failure", async () => {
    const { detail, work } = await load("inc-cartservice-0002");
    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.tone).toBe("waiting");
  });

  it("stops claiming to wait once a worker has claimed the item", async () => {
    const { detail, work } = await load("inc-checkout-0001");
    expect(isUnclaimedAnalysis(work.analysis)).toBe(false);
    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.awaitingAgentRuntime).toBe(false);
  });

  it("reports an in-flight Agent as analyzing, not waiting", async () => {
    const { detail, work } = await load("inc-cartservice-0002");
    const running = {
      ...work,
      analysis: {
        ...work.analysis!,
        state: "RUNNING" as const,
        attempt_count: 1,
        worker_id: "agent-worker-3",
      },
    };
    const diagnosis = deriveDiagnosis(detail.incident, running, []);
    expect(diagnosis.state).toBe("AGENT_ANALYZING");
    expect(diagnosis.awaitingAgentRuntime).toBe(false);
    expect(diagnosis.description).toContain("agent-worker-3");
  });
});

describe("Pipeline states without a Report", () => {
  it("explains a FAILED Incident", async () => {
    const { detail, work } = await load("inc-payment-0005");
    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.state).toBe("FAILED");
    expect(diagnosis.outcome).toBe("NOT_AVAILABLE");
  });

  it("explains a PARTIAL pipeline that never reached analysis", async () => {
    const { detail, work } = await load("inc-rediscart-0004");
    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.state).toBe("PIPELINE_PARTIAL");
    expect(diagnosis.outcome).toBe("NOT_AVAILABLE");
  });

  it("explains an Incident still collecting", async () => {
    const { detail, work } = await load("inc-adservice-0006");
    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.state).toBe("WAITING_COLLECTION");
  });

  it("explains an Incident still localizing", async () => {
    const { detail, work } = await load("inc-shipping-0007");
    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.state).toBe("WAITING_LOCALIZATION");
  });
});

describe("Controlled evaluation labelling", () => {
  it("returns null when no verification label is present", async () => {
    const { detail } = await load("inc-checkout-0001");
    // The Viewer must never guess from an alert name.
    expect(controlledVerificationId(detail.incident)).toBeNull();
  });

  it("returns the label verbatim when the API provides one", async () => {
    const { detail } = await load("inc-checkout-0001");
    const labelled = {
      ...detail.incident,
      alert: {
        ...detail.incident.alert,
        labels: { ...detail.incident.alert.labels, verification_id: "checkout-oom-1787790100" },
      },
    };
    expect(controlledVerificationId(labelled)).toBe("checkout-oom-1787790100");
  });
});


describe("PARTIAL copy matches the Report contract", () => {
  it("describes a PARTIAL Report that recorded a root cause", async () => {
    const { detail, work } = await load("inc-emailsvc-0008");
    expect(detail.reports[0].report.status).toBe("partial");
    expect(detail.reports[0].report.root_cause).not.toBeNull();

    const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
    expect(diagnosis.outcome).toBe("PARTIAL");
    expect(diagnosis.description).toBe(PARTIAL_WITH_ROOT_CAUSE);
    expect(diagnosis.description).toMatch(/A root cause was recorded/);
  });

  it("describes a PARTIAL Report that recorded no root cause", async () => {
    const { detail, work } = await load("inc-emailsvc-0008");
    const bundle = detail.reports[0];
    // A PARTIAL Report may legally omit a root cause when proof is incomplete.
    const withoutRootCause = {
      ...bundle,
      report: { ...bundle.report, root_cause: null },
    };

    const diagnosis = deriveDiagnosis(detail.incident, work, [withoutRootCause]);
    expect(diagnosis.outcome).toBe("PARTIAL");
    expect(diagnosis.description).toBe(PARTIAL_WITHOUT_ROOT_CAUSE);
    expect(diagnosis.description).not.toMatch(/A root cause was recorded/);
  });

  it("keeps the PARTIAL label and never calls it a pipeline failure", async () => {
    const { detail, work } = await load("inc-emailsvc-0008");
    const bundle = detail.reports[0];
    for (const rootCause of [bundle.report.root_cause, null]) {
      const diagnosis = deriveDiagnosis(detail.incident, work, [
        { ...bundle, report: { ...bundle.report, root_cause: rootCause } },
      ]);
      expect(diagnosis.outcome).toBe("PARTIAL");
      expect(diagnosis.state).toBe("PARTIAL");
      expect(diagnosis.tone).not.toBe("failed");
      expect(diagnosis.title).toBe("Partial diagnosis");
    }
  });

  it("does not confuse PARTIAL without a root cause with ABSTAIN", async () => {
    const { detail, work } = await load("inc-emailsvc-0008");
    const bundle = detail.reports[0];
    const diagnosis = deriveDiagnosis(detail.incident, work, [
      { ...bundle, report: { ...bundle.report, root_cause: null } },
    ]);
    // ABSTAIN is `inconclusive`; this Report is `partial`.
    expect(diagnosis.outcome).not.toBe("ABSTAIN");
  });
});
