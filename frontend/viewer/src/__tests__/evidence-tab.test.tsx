import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EvidenceTab } from "@/components/incident-detail/tabs/evidence-tab";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";
import { evidenceCompleteness } from "@/lib/evidence";
import type { EvidenceItem } from "@/lib/types";

const adapter = new FixtureViewerAdapter();

async function evidenceFor(incidentId: string): Promise<EvidenceItem[]> {
  return (await adapter.getIncidentDetail(incidentId)).evidence;
}

describe("EvidenceTab insufficient data", () => {
  it("labels an INSUFFICIENT_DATA item and states the cause", async () => {
    const evidence = await evidenceFor("inc-rediscart-0004");
    render(<EvidenceTab evidence={evidence} focusedEvidenceId={null} />);

    expect(screen.getByText("INSUFFICIENT_DATA")).toBeInTheDocument();
    expect(
      screen.getByText("Insufficient data — this Evidence supports no conclusion"),
    ).toBeInTheDocument();
    expect(screen.getByText("PROMETHEUS_QUERY_TIMEOUT")).toBeInTheDocument();
    expect(
      screen.getByText(/The range query exceeded its 10s deadline after 2 attempts/),
    ).toBeInTheDocument();
  });

  it("names the series that were expected but never returned", async () => {
    const evidence = await evidenceFor("inc-rediscart-0004");
    render(<EvidenceTab evidence={evidence} focusedEvidenceId={null} />);

    expect(screen.getByText("Missing series")).toBeInTheDocument();
    expect(
      screen.getByText(
        'redis_memory_used_bytes{namespace="online-boutique",pod="redis-cart-7f8d9c6b5-lk3jd"}',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/Expected samples:/)).toBeInTheDocument();
    expect(screen.getByText(/Returned samples:/)).toBeInTheDocument();
  });

  it("marks a partial item as a lower bound rather than a full observation", async () => {
    const evidence = await evidenceFor("inc-rediscart-0004");
    render(<EvidenceTab evidence={evidence} focusedEvidenceId={null} />);

    expect(
      screen.getByText("Partial data — treat these values as a lower bound"),
    ).toBeInTheDocument();
    expect(screen.getByText("LOG_BACKEND_PARTIAL_RESPONSE")).toBeInTheDocument();
  });

  it("lists redacted field paths without rendering any value", async () => {
    const evidence = await evidenceFor("inc-rediscart-0004");
    render(<EvidenceTab evidence={evidence} focusedEvidenceId={null} />);

    expect(
      screen.getByText(
        /Redacted before storage — values are not retained and cannot be shown/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("line.auth_token")).toBeInTheDocument();
  });

  it("keeps facts collapsed until the operator expands them", async () => {
    const evidence = await evidenceFor("inc-rediscart-0004");
    render(<EvidenceTab evidence={evidence} focusedEvidenceId={null} />);

    const toggles = screen.getAllByRole("button", { name: /facts/i });
    expect(toggles[0]).toHaveAttribute("aria-expanded", "false");
  });

  it("explains why there is no Evidence rather than showing a blank pane", () => {
    render(<EvidenceTab evidence={[]} focusedEvidenceId={null} />);
    expect(
      screen.getByText("No Evidence was stored for this Incident"),
    ).toBeInTheDocument();
  });
});

describe("evidenceCompleteness", () => {
  it("classifies fixture Evidence by its recorded status and completeness", async () => {
    const evidence = await evidenceFor("inc-rediscart-0004");
    const byId = Object.fromEntries(
      evidence.map((item) => [item.evidence_id, evidenceCompleteness(item)]),
    );
    expect(byId["ev-redis-mem-01"]).toBe("insufficient");
    expect(byId["ev-redis-log-01"]).toBe("partial");
    expect(byId["ev-redis-evt-01"]).toBe("complete");
  });
});
