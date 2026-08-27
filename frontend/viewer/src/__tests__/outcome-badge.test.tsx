import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RcaOutcomeBadge } from "@/components/status";
import { deriveDiagnosis, type RcaOutcome } from "@/lib/diagnosis";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";

const adapter = new FixtureViewerAdapter();

function tooltipFor(outcome: RcaOutcome): string {
  render(<RcaOutcomeBadge outcome={outcome} />);
  return screen.getByTitle(/./).getAttribute("title") ?? "";
}

describe("PARTIAL badge tooltip is contract-safe", () => {
  it("keeps the visible label as PARTIAL", () => {
    render(<RcaOutcomeBadge outcome="PARTIAL" />);
    expect(screen.getByText("PARTIAL")).toBeInTheDocument();
  });

  it("does not claim a root cause was recorded", () => {
    const tooltip = tooltipFor("PARTIAL");
    // A PARTIAL Report may legally carry root_cause=null, so the static
    // tooltip must hold for both forms.
    expect(tooltip).not.toMatch(/a root cause was recorded/i);
    expect(tooltip).toBe(
      "The investigation produced partial findings; an accepted root cause may still be unresolved.",
    );
  });

  it("is accurate for a PARTIAL Report that has no root cause", async () => {
    const detail = await adapter.getIncidentDetail("inc-emailsvc-0008");
    const bundle = detail.reports[0];
    const withoutRootCause = {
      ...bundle,
      report: { ...bundle.report, root_cause: null },
    };
    const work = await adapter.getIncidentWorkState("inc-emailsvc-0008");
    const diagnosis = deriveDiagnosis(detail.incident, work, [withoutRootCause]);

    expect(diagnosis.outcome).toBe("PARTIAL");
    const tooltip = tooltipFor(diagnosis.outcome);
    expect(tooltip).not.toMatch(/root cause was recorded/i);
  });
});

describe("other outcome semantics are unchanged", () => {
  it("PROVEN still states a supported root cause", () => {
    expect(tooltipFor("PROVEN")).toMatch(/root cause was recorded and supported/i);
  });

  it("ABSTAIN still states the Agent declined", () => {
    expect(tooltipFor("ABSTAIN")).toMatch(/declined to name a root cause/i);
  });

  it("AMBIGUOUS still states candidates were unsettled", () => {
    expect(tooltipFor("AMBIGUOUS")).toMatch(/none was settled on/i);
  });

  it("NOT_AVAILABLE still states no Report exists", () => {
    expect(tooltipFor("NOT_AVAILABLE")).toMatch(/No RCA Report has been stored/i);
  });

  it("renders NOT_AVAILABLE with its own label, not PARTIAL", () => {
    render(<RcaOutcomeBadge outcome="NOT_AVAILABLE" />);
    expect(screen.getByText("Not available")).toBeInTheDocument();
  });
});
