import { describe, expect, it } from "vitest";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";
import { ViewerApiError } from "@/lib/adapter/types";
import type { IncidentListQuery } from "@/lib/types";

const adapter = new FixtureViewerAdapter();

function query(overrides: Partial<IncidentListQuery> = {}): IncidentListQuery {
  return {
    schema_version: "1.0.0",
    statuses: [],
    severities: [],
    namespace: null,
    search: null,
    limit: 25,
    cursor: null,
    ...overrides,
  };
}

describe("FixtureViewerAdapter list filtering", () => {
  it("orders Incidents by updated_at descending", async () => {
    const result = await adapter.listIncidents(query());
    const updated = result.items.map((item) => item.updated_at);
    expect([...updated].sort().reverse()).toEqual(updated);
  });

  it("filters by status", async () => {
    const result = await adapter.listIncidents(query({ statuses: ["FAILED"] }));
    expect(result.items.length).toBeGreaterThan(0);
    expect(result.items.every((item) => item.status === "FAILED")).toBe(true);
  });

  it("filters by severity", async () => {
    const result = await adapter.listIncidents(query({ severities: ["critical"] }));
    expect(result.items.length).toBeGreaterThan(0);
    expect(result.items.every((item) => item.severity === "critical")).toBe(true);
  });

  it("combines status and severity as an intersection", async () => {
    const result = await adapter.listIncidents(
      query({ statuses: ["ANALYZING"], severities: ["critical"] }),
    );
    expect(
      result.items.every(
        (item) => item.status === "ANALYZING" && item.severity === "critical",
      ),
    ).toBe(true);
  });

  it("matches search against alert name and resource identity", async () => {
    const byAlert = await adapter.listIncidents(query({ search: "KubePodNotReady" }));
    expect(byAlert.items.map((item) => item.incident_id)).toContain("inc-checkout-0001");

    const byResource = await adapter.listIncidents(query({ search: "redis-cart" }));
    expect(byResource.items.length).toBeGreaterThan(0);
  });

  it("returns an empty page rather than an error when nothing matches", async () => {
    const result = await adapter.listIncidents(query({ search: "no-such-service" }));
    expect(result.items).toEqual([]);
    expect(result.next_cursor).toBeNull();
  });

  it("filters by namespace", async () => {
    const matching = await adapter.listIncidents(query({ namespace: "online-boutique" }));
    expect(matching.items.length).toBeGreaterThan(0);

    const other = await adapter.listIncidents(query({ namespace: "kube-system" }));
    expect(other.items).toEqual([]);
  });
});

describe("FixtureViewerAdapter cursor pagination", () => {
  it("walks forward without repeating Incidents", async () => {
    const first = await adapter.listIncidents(query({ limit: 5 }));
    expect(first.items).toHaveLength(5);
    expect(first.next_cursor).not.toBeNull();

    const second = await adapter.listIncidents(
      query({ limit: 5, cursor: first.next_cursor }),
    );
    const firstIds = first.items.map((item) => item.incident_id);
    const secondIds = second.items.map((item) => item.incident_id);
    expect(secondIds.some((id) => firstIds.includes(id))).toBe(false);
  });

  it("clears the cursor on the final page", async () => {
    let cursor: string | null = null;
    let pages = 0;
    do {
      const page: Awaited<ReturnType<typeof adapter.listIncidents>> =
        await adapter.listIncidents(query({ limit: 20, cursor }));
      cursor = page.next_cursor;
      pages += 1;
    } while (cursor && pages < 20);

    expect(cursor).toBeNull();
    expect(pages).toBeGreaterThan(1);
  });

  it("rejects a cursor replayed against different filters", async () => {
    const first = await adapter.listIncidents(query({ limit: 5 }));
    await expect(
      adapter.listIncidents(query({ limit: 5, cursor: first.next_cursor, statuses: ["FAILED"] })),
    ).rejects.toThrow(ViewerApiError);
  });
});

describe("FixtureViewerAdapter determinism", () => {
  it("returns identical payloads across repeated calls", async () => {
    const first = await adapter.listIncidents(query());
    const second = await adapter.listIncidents(query());
    expect(second).toEqual(first);
  });

  it("returns an identical detail bundle across repeated calls", async () => {
    const first = await adapter.getIncidentDetail("inc-checkout-0001");
    const second = await adapter.getIncidentDetail("inc-checkout-0001");
    expect(second).toEqual(first);
  });

  it("builds a chronologically ordered timeline", async () => {
    const detail = await adapter.getIncidentDetail("inc-checkout-0001");
    const times = detail.timeline.map((event) => event.occurred_at);
    expect([...times].sort()).toEqual(times);
  });
});

describe("FixtureViewerAdapter reads", () => {
  it("exposes work state without a claim token", async () => {
    const work = await adapter.getIncidentWorkState("inc-cartservice-0002");
    expect(work.analysis?.state).toBe("READY");
    expect(work.analysis?.context_id).toBe("ctx-cart-0002");
    expect(Object.keys(work.analysis ?? {})).not.toContain("claim_token");
  });

  it("reports a missing Incident as not-found", async () => {
    await expect(adapter.getIncidentDetail("inc-missing-0000")).rejects.toMatchObject({
      kind: "not-found",
    });
  });

  it("exposes no mutating members", () => {
    const surface = [
      ...Object.getOwnPropertyNames(Object.getPrototypeOf(adapter)),
    ].filter((name) => name !== "constructor");
    expect(surface.sort()).toEqual([
      "getIncidentDetail",
      "getIncidentWorkState",
      "listIncidents",
    ]);
  });
});
