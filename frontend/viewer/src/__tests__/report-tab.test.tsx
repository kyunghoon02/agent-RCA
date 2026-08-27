import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReportTab } from "@/components/incident-detail/tabs/report-tab";
import { WorkQueueCards } from "@/components/incident-detail/work-queue-cards";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";
import { isWaitingForAgentRuntime } from "@/lib/work";

const adapter = new FixtureViewerAdapter();

async function load(incidentId: string) {
  const [detail, work] = await Promise.all([
    adapter.getIncidentDetail(incidentId),
    adapter.getIncidentWorkState(incidentId),
  ]);
  return { detail, work };
}

function renderReport(
  detail: Awaited<ReturnType<typeof adapter.getIncidentDetail>>,
  work: Awaited<ReturnType<typeof adapter.getIncidentWorkState>> | null,
) {
  return render(
    <ReportTab
      incident={detail.incident}
      reports={detail.reports}
      agentRuns={detail.agent_runs}
      work={work}
      onFocusEvidence={() => {}}
    />,
  );
}

describe("Agent-disabled empty state", () => {
  it("derives the waiting state from an unclaimed analysis work item", async () => {
    const { detail, work } = await load("inc-cartservice-0002");
    expect(detail.incident.status).toBe("ANALYZING");
    expect(work.analysis?.state).toBe("READY");
    expect(isWaitingForAgentRuntime(detail.incident, work)).toBe(true);
  });

  it("shows the Agent-runtime message instead of a Report", async () => {
    const { detail, work } = await load("inc-cartservice-0002");
    renderReport(detail, work);

    // Copy is now the shared diagnosis wording, which names the queue state
    // rather than asserting anything about a runtime being "disabled".
    expect(screen.getByText("Waiting for Agent runtime")).toBeInTheDocument();
    expect(
      screen.getByText(/no Agent runtime has claimed this analysis work/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/ctx-cart-0002/)).toBeInTheDocument();
    expect(screen.getByText(/This is a waiting state, not a failure/)).toBeInTheDocument();
  });

  it("presents the waiting work card without failure styling", async () => {
    const { detail, work } = await load("inc-cartservice-0002");
    const { container } = render(
      <WorkQueueCards incident={detail.incident} work={work} isLoading={false} />,
    );

    expect(screen.getByText("Waiting for Agent runtime")).toBeInTheDocument();
    // The waiting notice must not borrow the critical (error) surface.
    expect(container.querySelector(".bg-status-critical-surface")).toBeNull();
  });

  it("does not claim to be waiting once a worker has claimed the item", async () => {
    const { detail, work } = await load("inc-checkout-0001");
    expect(isWaitingForAgentRuntime(detail.incident, work)).toBe(false);
  });
});

describe("ABSTAIN report", () => {
  it("labels an inconclusive report with no root cause as ABSTAIN", async () => {
    const { detail, work } = await load("inc-frontend-0003");
    renderReport(detail, work);

    expect(screen.getAllByText("ABSTAIN").length).toBeGreaterThan(0);
    expect(screen.getByText("The Agent abstained")).toBeInTheDocument();
    expect(
      screen.getByText(/This is the intended outcome of the Evidence gate, not an error/),
    ).toBeInTheDocument();
  });

  it("states plainly that no root cause was given, and why", async () => {
    const { detail, work } = await load("inc-frontend-0003");
    renderReport(detail, work);

    expect(
      screen.getByText("No root cause was stated for this Incident."),
    ).toBeInTheDocument();
    expect(screen.getByText(/trace-summary for the 22:38Z window/)).toBeInTheDocument();
  });

  it("marks the Report read-only and offers no remediation control", async () => {
    const { detail, work } = await load("inc-frontend-0003");
    const { container } = renderReport(detail, work);

    expect(screen.getByText("read-only")).toBeInTheDocument();
    expect(
      screen.getByText("Suggestions only. This Viewer cannot apply any of them."),
    ).toBeInTheDocument();

    // No control anywhere in the Report may offer to act on the cluster.
    const actions = [...container.querySelectorAll("button")].map(
      (button) => button.textContent?.toLowerCase() ?? "",
    );
    for (const verb of ["restart", "rollback", "apply", "remediate", "execute", "delete"]) {
      expect(actions.some((label) => label.includes(verb))).toBe(false);
    }
  });
});

describe("Report unavailable states", () => {
  it("explains a FAILED Incident that never reached analysis", async () => {
    const { detail, work } = await load("inc-payment-0005");
    renderReport(detail, work);
    expect(
      screen.getByText("No Report — the Incident failed before analysis"),
    ).toBeInTheDocument();
  });

  it("explains a PARTIAL Incident whose Context was never analysed", async () => {
    const { detail, work } = await load("inc-rediscart-0004");
    renderReport(detail, work);
    expect(
      screen.getByText("No Report — the Incident ended as PARTIAL"),
    ).toBeInTheDocument();
  });
});

describe("Conclusive report", () => {
  it("shows the root cause, budget and Agent run accounting", async () => {
    const { detail, work } = await load("inc-checkout-0001");
    renderReport(detail, work);

    // Rendered twice by design: the outcome badge and the pipeline-vs-outcome line.
    expect(screen.getAllByText("PROVEN").length).toBeGreaterThan(0);
    expect(screen.getByText(/Deployment revision 8 added an envFrom reference/)).toBeInTheDocument();
    expect(screen.getByText("gpt-4.1-mini")).toBeInTheDocument();
    expect(screen.getByText("arun-checkout-0001")).toBeInTheDocument();
  });
});
