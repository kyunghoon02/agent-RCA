import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { IncidentTable } from "@/components/incidents/incident-table";
import type { IncidentStatus, IncidentSummary, KubernetesEntityRef } from "@/lib/types";

/** Groupable: a stable cluster_id + uid pair. */
function entity(uid = "uid-1"): KubernetesEntityRef {
  return {
    api_version: "v1",
    cluster_id: "cluster-a",
    kind: "Pod",
    namespace: "online-boutique",
    name: "checkoutservice",
    uid,
    exists: true,
  };
}

let sequence = 0;
function incident(overrides: Partial<IncidentSummary> = {}): IncidentSummary {
  sequence += 1;
  return {
    incident_id: `inc-tablecase-${String(sequence).padStart(6, "0")}`,
    status: "ANALYZING",
    severity: "critical",
    source: "alertmanager",
    triggered_at: "2026-08-27T00:00:00Z",
    updated_at: "2026-08-27T00:00:00Z",
    alert_name: "AgentRCAControlledCheckoutOOM",
    source_entity: entity(),
    collector_problem_count: 0,
    ...overrides,
  };
}

function renderTable(items: IncidentSummary[]) {
  return render(
    <IncidentTable items={items} selectedId={null} onSelect={() => {}} collapseRepeats />,
  );
}

/** The grouped row is the one carrying the repeat count. */
function groupRow(): HTMLElement {
  const label = screen.getByText(/repeated runs on this entity/);
  return label.closest("tr")!;
}

describe("repeat group collector problems", () => {
  it("shows the summed problem count, not the repeat count", () => {
    renderTable([
      incident({ collector_problem_count: 2 }),
      incident({ collector_problem_count: 3 }),
      incident({ collector_problem_count: 4 }),
    ]);

    const row = groupRow();
    // Three repeats summing to nine: the cell must read 9, never 3.
    expect(within(row).getByText("9")).toBeInTheDocument();
    expect(within(row).queryByText("×3")).not.toBeInTheDocument();
  });

  it("renders 0 like a normal row when no member has problems", () => {
    renderTable([incident(), incident()]);
    expect(within(groupRow()).getByText("0")).toBeInTheDocument();
  });

  it("keeps the repeat count in the Alert column only", () => {
    renderTable([incident(), incident(), incident()]);
    expect(screen.getByText("3 repeated runs on this entity")).toBeInTheDocument();
  });
});

describe("mixed-status repeat groups", () => {
  const mixed: IncidentStatus[] = [
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

  it("summarises a mixed group instead of showing the newest member's status", () => {
    renderTable(mixed.map((status) => incident({ status })));
    const row = groupRow();

    expect(within(row).getByText("Mixed")).toBeInTheDocument();
    expect(within(row).getByText(/5 ANALYZING/)).toBeInTheDocument();
    expect(within(row).getByText(/3 FAILED/)).toBeInTheDocument();
    expect(within(row).getByText(/1 REPORTED/)).toBeInTheDocument();
  });

  it("reports how many members actually have a Report", () => {
    renderTable(mixed.map((status) => incident({ status })));
    const row = groupRow();
    // The newest member is REPORTED, but only one of nine has a Report.
    expect(within(row).getByText("1 report")).toBeInTheDocument();
    expect(within(row).getByText("/ 9 runs")).toBeInTheDocument();
  });

  it("keeps a single status badge for a homogeneous group", () => {
    renderTable([
      incident({ status: "ANALYZING" }),
      incident({ status: "ANALYZING" }),
    ]);
    const row = groupRow();
    expect(within(row).queryByText("Mixed")).not.toBeInTheDocument();
    expect(within(row).getByText("ANALYZING")).toBeInTheDocument();
    expect(within(row).getByText("Pending")).toBeInTheDocument();
  });

  it("makes every member reachable by expanding", () => {
    const items = mixed.map((status) => incident({ status }));
    renderTable(items);

    fireEvent.click(screen.getByRole("button", { expanded: false }));
    for (const item of items) {
      expect(screen.getByText(item.incident_id)).toBeInTheDocument();
    }
  });
});

describe("identity safety in the rendered table", () => {
  it("does not fold Incidents that lack a stable UID", () => {
    renderTable([
      incident({ source_entity: { ...entity(), uid: null } }),
      incident({ source_entity: { ...entity(), uid: null } }),
    ]);
    expect(screen.queryByText(/repeated runs on this entity/)).not.toBeInTheDocument();
  });

  it("does not fold Incidents from different clusters", () => {
    renderTable([
      incident({ source_entity: { ...entity(), cluster_id: "cluster-a" } }),
      incident({ source_entity: { ...entity(), cluster_id: "cluster-b" } }),
    ]);
    expect(screen.queryByText(/repeated runs on this entity/)).not.toBeInTheDocument();
  });
});
