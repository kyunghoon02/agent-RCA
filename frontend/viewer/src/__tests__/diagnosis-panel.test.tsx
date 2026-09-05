import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiagnosisPanel } from "@/components/incident-detail/diagnosis-panel";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";
import { deriveDiagnosis } from "@/lib/diagnosis";
import type { RcaReport } from "@/lib/types";

const adapter = new FixtureViewerAdapter();

async function renderPanel(incidentId: string, report?: RcaReport) {
  const [detail, work] = await Promise.all([
    adapter.getIncidentDetail(incidentId),
    adapter.getIncidentWorkState(incidentId),
  ]);
  if (report) detail.reports = [{ report, markdown: "" }];
  const diagnosis = deriveDiagnosis(detail.incident, work, detail.reports);
  const result = render(
    <DiagnosisPanel
      incident={detail.incident}
      work={work}
      reports={detail.reports}
      diagnosis={diagnosis}
      onOpenReport={() => {}}
      onFocusEvidence={() => {}}
    />,
  );
  return { ...result, detail, work };
}

describe("Waiting-for-Agent panel", () => {
  it("states the required title and description", async () => {
    await renderPanel("inc-cartservice-0002");
    expect(screen.getByText("Waiting for Agent runtime")).toBeInTheDocument();
    expect(
      screen.getByText(
        "The Frozen Context is ready and pinned, but no Agent runtime has claimed this analysis work.",
      ),
    ).toBeInTheDocument();
  });

  it("shows pipeline, work state, Context, Report and last update separately", async () => {
    await renderPanel("inc-cartservice-0002");
    // Three distinct facts that must never be conflated.
    expect(screen.getByText("Pipeline")).toBeInTheDocument();
    expect(screen.getByText("ANALYZING")).toBeInTheDocument();
    expect(screen.getByText("Analysis work")).toBeInTheDocument();
    expect(screen.getByText("READY")).toBeInTheDocument();
    expect(screen.getByText("ctx-cart-0002")).toBeInTheDocument();
    // "Not available" appears twice by design: the outcome badge and the
    // Report field, which are separate facts that happen to agree here.
    expect(screen.getAllByText("Not available")).toHaveLength(2);
    expect(screen.getByText("Last update")).toBeInTheDocument();
  });

  it("shows no root cause, confidence or supporting Evidence without a Report", async () => {
    const { container } = await renderPanel("inc-cartservice-0002");
    expect(screen.queryByText(/Root cause/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Evidence cited/i)).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/confidence/i);
    expect(screen.queryByRole("button", { name: /Open RCA Report/ })).not.toBeInTheDocument();
  });

  it("says an Agent has not claimed the work rather than that a runtime is down", async () => {
    await renderPanel("inc-cartservice-0002");
    expect(screen.getByText("No Agent runtime has claimed this")).toBeInTheDocument();
  });
});

describe("Panel with a stored Report", () => {
  it("shows the outcome, root cause and cited/missing counts", async () => {
    await renderPanel("inc-checkout-0001");
    expect(screen.getByText("PROVEN")).toBeInTheDocument();
    expect(screen.getByText("Root cause")).toBeInTheDocument();
    expect(screen.getByText(/Deployment revision 8 added an envFrom reference/)).toBeInTheDocument();
    expect(screen.getByText(/Evidence cited/)).toBeInTheDocument();
    expect(screen.getByText("Selected-cause requirements unavailable")).toBeInTheDocument();
    expect(screen.getByText("1 hypothesis requirements missing")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Open RCA Report/ })).toBeInTheDocument();
  });

  it("separates selected-cause requirements from unresolved alternatives", async () => {
    const detail = await adapter.getIncidentDetail("inc-checkout-0001");
    const report = structuredClone(detail.reports[0].report);
    report.root_cause!.cause_id = "kubernetes.missing-configmap";
    report.hypotheses[0].cause_id = "kubernetes.missing-configmap";
    report.hypotheses[1].missing_evidence = ["node pressure observation"];
    report.hypotheses[1].status = "unresolved";
    report.hypotheses[2].status = "unresolved";
    await renderPanel("inc-checkout-0001", report);
    expect(screen.getByText("PROVEN")).toBeInTheDocument();
    expect(screen.getByText("0 selected-cause requirements missing")).toBeInTheDocument();
    expect(screen.getByText("2 alternative-hypothesis requirements missing")).toBeInTheDocument();
    expect(screen.queryByText(/Evidence missing/)).not.toBeInTheDocument();
  });

  it("presents an ABSTAIN outcome without a root cause statement", async () => {
    await renderPanel("inc-frontend-0003");
    expect(screen.getByText("ABSTAIN")).toBeInTheDocument();
    expect(screen.getByText("Agent abstained")).toBeInTheDocument();
    expect(screen.getByText("2 hypothesis requirements missing")).toBeInTheDocument();
    expect(screen.queryByText(/selected-cause requirements/i)).not.toBeInTheDocument();
    expect(
      screen.getByText("No root cause was recorded for this Incident."),
    ).toBeInTheDocument();
  });
});

describe("Panel for terminal pipelines", () => {
  it("reports a FAILED pipeline with no outcome", async () => {
    await renderPanel("inc-payment-0005");
    expect(screen.getByText("Pipeline failed")).toBeInTheDocument();
    expect(screen.getAllByText("Not available").length).toBeGreaterThan(0);
  });
});
