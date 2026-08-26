import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { IncidentListQuery, IncidentListResult } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  listIncidents: vi.fn(),
  mode: { value: "fixture" as "fixture" | "live" },
}));

vi.mock("@/lib/adapter", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/adapter")>();
  return {
    ...actual,
    getViewerAdapter: () => ({
      get mode() {
        return mocks.mode.value;
      },
      listIncidents: mocks.listIncidents,
      getIncidentDetail: vi.fn(),
      getIncidentWorkState: vi.fn(),
    }),
  };
});

const { IncidentListView } = await import("@/components/incidents/incident-list-view");
const { FixtureViewerAdapter } = await import("@/lib/adapter/fixture-adapter");

const fixtures = new FixtureViewerAdapter();

function baseQuery(): IncidentListQuery {
  return {
    schema_version: "1.0.0",
    statuses: [],
    severities: [],
    namespace: null,
    search: null,
    limit: 25,
    cursor: null,
  };
}

async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Last query the view sent to the adapter. */
function lastQuery(): IncidentListQuery {
  const calls = mocks.listIncidents.mock.calls;
  return calls[calls.length - 1][0] as IncidentListQuery;
}

describe("IncidentListView filtering", () => {
  beforeEach(async () => {
    vi.useFakeTimers();
    mocks.mode.value = "fixture";
    mocks.listIncidents.mockReset();
    mocks.listIncidents.mockImplementation((query: IncidentListQuery) =>
      fixtures.listIncidents(query),
    );
  });
  afterEach(() => vi.useRealTimers());

  it("loads the first page with an unfiltered query", async () => {
    render(<IncidentListView />);
    await flush();

    expect(lastQuery()).toEqual(baseQuery());
    expect(screen.getByText("KubePodNotReady")).toBeInTheDocument();
  });

  it("sends the search term only after Apply is pressed", async () => {
    render(<IncidentListView />);
    await flush();

    fireEvent.change(screen.getByLabelText("Alert or resource"), {
      target: { value: "cartservice" },
    });
    await flush();
    expect(lastQuery().search).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await flush();

    expect(lastQuery().search).toBe("cartservice");
    expect(screen.getByText("CartServiceHighLatency")).toBeInTheDocument();
    expect(screen.queryByText("KubePodNotReady")).not.toBeInTheDocument();
  });

  it("sends the namespace filter", async () => {
    render(<IncidentListView />);
    await flush();

    fireEvent.change(screen.getByLabelText("Namespace"), {
      target: { value: "online-boutique" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await flush();

    expect(lastQuery().namespace).toBe("online-boutique");
  });

  it("applies a status filter from the summary cards", async () => {
    render(<IncidentListView />);
    await flush();

    fireEvent.click(screen.getByRole("button", { name: /Failed/ }));
    await flush();

    expect(lastQuery().statuses).toEqual(["FAILED"]);
    expect(screen.getByText("PaymentServiceDown")).toBeInTheDocument();
  });

  it("resets pagination when filters change", async () => {
    render(<IncidentListView />);
    await flush();

    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    await flush();
    expect(lastQuery().cursor).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Reported/ }));
    await flush();

    // A cursor is bound to the filters that produced it, so changing filters
    // must start a fresh chain rather than replay a rejected cursor.
    expect(lastQuery().cursor).toBeNull();
    expect(lastQuery().statuses).toEqual(["REPORTED"]);
  });

  it("clears every filter at once", async () => {
    render(<IncidentListView />);
    await flush();

    fireEvent.click(screen.getByRole("button", { name: /Analyzing/ }));
    await flush();

    fireEvent.click(screen.getByRole("button", { name: /Clear 1/ }));
    await flush();

    expect(lastQuery()).toEqual(baseQuery());
  });

  it("explains an empty result instead of showing a blank table", async () => {
    render(<IncidentListView />);
    await flush();

    fireEvent.change(screen.getByLabelText("Alert or resource"), {
      target: { value: "no-such-service" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await flush();

    expect(screen.getByText("No Incidents match these filters")).toBeInTheDocument();
  });

  it("badges fixture data as Demo Data", async () => {
    render(<IncidentListView />);
    await flush();
    expect(
      screen.getByText("Demo Data — no Viewer API is configured"),
    ).toBeInTheDocument();
  });
});

describe("IncidentListView resilience", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.mode.value = "live";
    mocks.listIncidents.mockReset();
  });
  afterEach(() => vi.useRealTimers());

  it("keeps the previous rows on screen when a refresh fails", async () => {
    const page = (await fixtures.listIncidents(baseQuery())) as IncidentListResult;
    mocks.listIncidents
      .mockResolvedValueOnce(page)
      .mockRejectedValue(new Error("Viewer API is unreachable"));

    render(<IncidentListView />);
    await flush();
    expect(screen.getByText("KubePodNotReady")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh now" }));
    await flush();

    // Rows survive the failure, and the failure is stated rather than hidden.
    expect(screen.getByText("KubePodNotReady")).toBeInTheDocument();
    expect(
      screen.getByText("Showing the last successful result"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Viewer API is unreachable/)).toBeInTheDocument();
  });

  it("reports a disconnected API when nothing has loaded yet", async () => {
    mocks.listIncidents.mockRejectedValue(new Error("Viewer API is unreachable"));

    render(<IncidentListView />);
    await flush();

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Viewer API is unreachable");
    expect(
      screen.queryByText("Showing the last successful result"),
    ).not.toBeInTheDocument();
  });

  it("stops issuing requests once polling is paused", async () => {
    const page = (await fixtures.listIncidents(baseQuery())) as IncidentListResult;
    mocks.listIncidents.mockResolvedValue(page);

    render(<IncidentListView />);
    await flush();

    fireEvent.click(screen.getByRole("button", { name: /Pause/ }));
    await flush();
    const afterPause = mocks.listIncidents.mock.calls.length;

    await flush(10_000);

    expect(mocks.listIncidents.mock.calls.length).toBe(afterPause);
    expect(screen.getByText("KubePodNotReady")).toBeInTheDocument();
  });
});
